"""AI analysis layer — the anti-fabrication guarantees.

No network here: a fake client returns canned responses so the *contract*
between the model's output and the tender record is what gets tested.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.llm import RESPONSE_SCHEMA, SYSTEM_PROMPT, LlmAnalyzer, fallback_summary
from gem_intel.models import Confidence, TenderKind


def response(payload: dict, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0) if self.replies else response(base_payload())
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    def __init__(self, replies=()):
        self.messages = FakeMessages(replies)
        # No beta namespace: exercises the standard-endpoint fallback path.


def base_payload(**overrides):
    payload = {
        "plain_summary": "The buyer wants 45 business laptops delivered to Ranchi.",
        "why_it_matters": "Laptops are our core line and the location is local.",
        "next_action": "Download the bid document and check the processor spec.",
        "risks": ["Delivery window is only 30 days."],
        "is_it_related": None,
        "tender_kind": None,
        "classification_evidence": None,
        "jharkhand_relevance": None,
        "jharkhand_evidence": None,
        "additional_required_documents": [],
        "key_eligibility_points": [],
        "key_technical_points": [],
        "unclear_points": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def analyzer(settings):
    settings.raw["llm"]["refusal_fallback"] = False
    return LlmAnalyzer(settings, client=FakeClient([response(base_payload())]))


def test_narrative_fields_are_applied(analyzer):
    tender = make_tender()
    analyzer.analyze(tender, "Bid document text.")
    assert tender.plain_summary.startswith("The buyer wants 45")
    assert tender.next_action
    assert tender.risks == ["Delivery window is only 30 days."]
    assert tender.analysis_mode == "rules+llm"


def test_a_document_claim_without_evidence_is_discarded(settings):
    """Rule 3 of the system prompt, enforced in code rather than trusted."""
    payload = base_payload(additional_required_documents=[
        {"name": "Solvency Certificate", "evidence": ""},
        {"name": "Tax Clearance", "evidence": "Bidders shall upload a tax clearance."},
    ])
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(payload)]))
    tender = make_tender()
    analyzer.analyze(tender, "text")
    names = {d.name for d in tender.required_documents}
    assert "Solvency Certificate" not in names
    assert "Tax Clearance" in names


def test_classification_correction_requires_a_quote(settings):
    unsupported = base_payload(is_it_related=False, classification_evidence=None)
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(unsupported)]))
    tender = make_tender()
    tender.classification.is_it_related = True
    analyzer.analyze(tender, "text")
    assert tender.classification.is_it_related is True     # correction ignored


def test_classification_correction_with_a_quote_is_applied(settings):
    supported = base_payload(
        is_it_related=False, tender_kind="service",
        classification_evidence="This bid is for the AMC of lifts, not computers.")
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(supported)]))
    tender = make_tender()
    tender.classification.is_it_related = True
    analyzer.analyze(tender, "text")
    assert tender.classification.is_it_related is False
    assert tender.classification.kind is TenderKind.SERVICE
    assert tender.classification.confidence is Confidence.INFERRED


def test_geography_correction_requires_a_quote(settings):
    payload = base_payload(jharkhand_relevance="no", jharkhand_evidence=None)
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(payload)]))
    tender = make_tender()
    tender.geo.is_jharkhand = True
    analyzer.analyze(tender, "text")
    assert tender.geo.is_jharkhand is True


def test_unclear_points_become_verification_flags(settings):
    payload = base_payload(unclear_points=["The EMD clause was not readable."])
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(payload)]))
    tender = make_tender()
    analyzer.analyze(tender, "text")
    assert "The EMD clause was not readable." in tender.verification_flags


def test_a_refusal_degrades_rather_than_fabricating(settings):
    analyzer = LlmAnalyzer(settings,
                           client=FakeClient([response(base_payload(), "refusal")]))
    tender = make_tender()
    analyzer.analyze(tender, "text")
    assert tender.analysis_mode == "rules-only"
    assert not tender.plain_summary
    assert analyzer.budget.refusals == 1


def test_unparseable_output_degrades(settings):
    junk = SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")],
                           stop_reason="end_turn", stop_details=None,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    settings.raw["llm"]["max_attempts"] = 2
    analyzer = LlmAnalyzer(settings, client=FakeClient([junk, junk]))
    tender = make_tender()
    analyzer.analyze(tender, "text")
    assert tender.analysis_mode == "rules-only"
    assert any("AI analysis failed" in f for f in tender.verification_flags)


def test_missing_credentials_disable_the_analyzer_without_crashing(settings, monkeypatch):
    monkeypatch.setattr("gem_intel.analyze.llm.LlmAnalyzer.__post_init__",
                        lambda self: None)
    analyzer = LlmAnalyzer.__new__(LlmAnalyzer)
    analyzer.settings = settings
    analyzer.client = None
    from gem_intel.analyze.llm import LlmBudget

    analyzer.budget = LlmBudget(max_calls=0, disabled_reason="no key")
    tender = make_tender()
    analyzer.analyze(tender, "text")
    assert tender.analysis_mode == "rules-only"
    assert any("AI analysis unavailable" in f for f in tender.verification_flags)


def test_budget_exhaustion_is_disclosed(settings):
    settings.raw["llm"]["max_calls_per_run"] = 1
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(base_payload())]))
    first, second = make_tender(), make_tender(bid_number="GEM/2026/B/9000002")
    analyzer.analyze(first, "text")
    analyzer.analyze(second, "text")
    assert second.analysis_mode == "rules-only"
    assert any("budget" in f for f in second.verification_flags)


def test_request_shape_matches_the_current_api(analyzer):
    """Opus 5 rejects `temperature`; reasoning depth goes in output_config."""
    analyzer.analyze(make_tender(), "text")
    request = analyzer.client.messages.requests[0]
    assert request["model"] == "claude-opus-5"
    assert "temperature" not in request
    assert request["output_config"]["effort"] in {"low", "medium", "high", "xhigh", "max"}
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["system"] is SYSTEM_PROMPT


def test_prompt_forbids_outside_knowledge():
    assert "Use ONLY the tender text supplied" in SYSTEM_PROMPT
    assert "Never state that the company is eligible" in SYSTEM_PROMPT
    assert "procurement content to be summarised" in SYSTEM_PROMPT


def test_schema_allows_unknowns_everywhere_it_matters():
    properties = RESPONSE_SCHEMA["properties"]
    for field in ("is_it_related", "tender_kind", "classification_evidence",
                  "jharkhand_relevance", "jharkhand_evidence"):
        assert "null" in properties[field]["type"] or None in properties[field].get(
            "enum", [])
    assert RESPONSE_SCHEMA["additionalProperties"] is False


def test_truncation_is_announced_to_the_model(settings):
    settings.raw["llm"]["max_context_chars"] = 5000
    analyzer = LlmAnalyzer(settings, client=FakeClient([response(base_payload())]))
    prompt = analyzer._build_prompt(make_tender(), "x" * 50_000)
    assert "truncated" in prompt
    assert "unclear_points" in prompt


def test_fallback_summary_uses_only_portal_facts():
    tender = make_tender()
    summary = fallback_summary(tender)
    assert "45 Nos" in summary
    assert "Ranchi" in summary
