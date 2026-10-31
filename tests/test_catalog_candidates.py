"""The catalog's own claims: size, uniqueness, families, determinism.

These tests only look at the *declaration*.  Nothing here runs a simulation, and
no number here says anything about a market.
"""

from __future__ import annotations

from axiom_forex.strategies.catalog.candidates import (
    FAMILIES,
    TIMEFRAMES,
    build_candidates,
    by_id,
    by_structure,
    candidate_id_for,
    catalog,
    filter_candidates,
    signature_of,
    summary_counts,
    timeframes_for,
)
from axiom_forex.strategies.catalog.primitives import ENTRIES, EXITS, FILTERS


def test_catalog_is_large_enough_and_every_row_is_unique():
    items = catalog()
    assert len(items) >= 1000
    assert len({item.candidate_id for item in items}) == len(items)
    assert len({item.signature for item in items}) == len(items)
    assert len({item.structure for item in items}) > 100


def test_structures_are_reported_separately_from_parameter_variants():
    counts = summary_counts()
    assert counts["distinct_rule_structures"] < counts["candidates"]
    assert counts["candidates"] == counts["distinct_signatures"]
    assert counts["distinct_rule_structures"] == len({item.structure for item in catalog()})
    per_structure = counts["parameter_variants_per_structure"]
    assert per_structure["max"] >= 2
    # No renamed padding: a structure with one parameter set is still one candidate.
    assert per_structure["min"] == 1


def test_every_declared_family_is_present():
    items = catalog()
    families = {item.family for item in items}
    assert families == {key for key, _label in FAMILIES}
    assert summary_counts()["families"]["multi_timeframe"]["candidates"] > 0


def test_catalog_is_deterministic_and_ids_do_not_depend_on_position():
    first = build_candidates()
    second = build_candidates()
    assert [item.candidate_id for item in first] == [item.candidate_id for item in second]
    item = first[17]
    assert by_id(item.candidate_id) is item or by_id(item.candidate_id).candidate_id == item.candidate_id
    assert candidate_id_for(item.signature) == item.candidate_id
    assert len(by_structure(item.structure)) >= 1


def test_grids_are_declared_not_improvised():
    assert len(ENTRIES) >= 10
    assert {item.name for item in EXITS} >= {"bracket", "trailing_atr", "time_exit", "ma_return"}
    assert {item.name for item in FILTERS} >= {"none", "htf_trend", "session"}
    for block in (*ENTRIES, *EXITS, *FILTERS):
        for params in block.variants():
            assert isinstance(params, dict)


def test_multi_timeframe_rows_are_only_offered_where_a_coarse_bar_exists():
    for item in catalog():
        offered = timeframes_for(item)
        assert offered, item.candidate_id
        assert set(offered) <= set(TIMEFRAMES)
        if item.filter_kind.needs_coarse:
            assert "4h" not in offered or int(item.filter_params.get("ratio", 4)) <= 1


def test_every_candidate_explains_itself():
    for item in catalog()[:200]:
        view = item.audit_view()
        assert view["summary"]
        assert view["entry_text"] and view["exit_text"] and view["filter_text"]
        assert "position-level exit" in view["risk"]
        assert view["candidate_id"].startswith("cat-")
        assert signature_of(
            item.entry, item.exit, item.filter, item.parameter_map
        ) == item.signature


def test_filter_candidates_keeps_catalog_order():
    items = catalog()
    subset = filter_candidates(items, families=("breakout",))
    assert subset
    assert all(item.family == "breakout" for item in subset)
    assert [item.candidate_id for item in subset] == [
        item.candidate_id for item in items if item.family == "breakout"
    ]
