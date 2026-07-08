"""Unit tests for build_competitor_candidates — pure function, no network calls."""
import pytest
from agents.candidates import build_competitor_candidates


STRIPE = {
    "name": "Stripe",
    "pricing_model": "2.9% + 30¢ per successful card transaction",
    "market_positioning": "Developer-first payments platform with extensive API coverage",
    "key_differentiators": ["instant payouts", "global reach", "strong DX"],
    "confidence_notes": "TechCrunch, 2024-06-01",
}

STRIPE_WITH_CLAIM = {
    **STRIPE,
    "claim": {
        "schema_version": 2,
        "entity_key": "organization:stripe",
        "attribute": "pricing.processing_rate",
        "value": {"basis_points": 290, "minor_units": 30, "currency": "USD"},
    },
}

MINIMAL = {
    "name": "Acme",
}


def test_returns_tuple():
    raw_text, candidates = build_competitor_candidates(STRIPE, "techcrunch", "2024-06-01")
    assert isinstance(raw_text, str)
    assert isinstance(candidates, list)


def test_pricing_candidate_always_present():
    _, candidates = build_competitor_candidates(STRIPE, "techcrunch", "2024-06-01")
    kinds = [c["kind"] for c in candidates]
    assert "domain_fact" in kinds
    pricing = next(c for c in candidates if c["metadata"]["fact"] == "pricing")
    assert "Stripe" in pricing["text"]
    assert "2.9%" in pricing["text"]


def test_positioning_and_differentiators_emitted():
    _, candidates = build_competitor_candidates(STRIPE, "techcrunch", "2024-06-01")
    facts = {c["metadata"]["fact"] for c in candidates}
    assert "positioning" in facts
    assert "differentiators" in facts


def test_claim_block_attached_when_present():
    _, candidates = build_competitor_candidates(STRIPE_WITH_CLAIM, "techcrunch", "2024-06-01")
    pricing = next(c for c in candidates if c["metadata"]["fact"] == "pricing")
    assert "claim" in pricing
    assert pricing["claim"]["attribute"] == "pricing.processing_rate"


def test_no_claim_block_without_source_claim():
    _, candidates = build_competitor_candidates(STRIPE, "techcrunch", "2024-06-01")
    pricing = next(c for c in candidates if c["metadata"]["fact"] == "pricing")
    assert "claim" not in pricing


def test_minimal_competitor_no_crash():
    raw_text, candidates = build_competitor_candidates(MINIMAL, "source", "2024-01-01")
    assert isinstance(candidates, list)
    assert len(candidates) >= 1  # pricing candidate always emitted


def test_source_label_in_metadata():
    _, candidates = build_competitor_candidates(STRIPE, "bloomberg", "2024-01-01")
    for c in candidates:
        assert c["metadata"]["source"] == "bloomberg"


def test_raw_text_contains_key_fields():
    raw_text, _ = build_competitor_candidates(STRIPE, "techcrunch", "2024-06-01")
    assert "Stripe" in raw_text
    assert "2.9%" in raw_text
    assert "techcrunch" in raw_text
