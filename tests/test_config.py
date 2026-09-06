"""Configuration loading and environment overrides."""

from __future__ import annotations

import pytest

from gem_intel.config import ConfigError, load_settings


def test_defaults_load(settings):
    assert settings.timezone == "Asia/Kolkata"
    assert settings.max_days_remaining == 10
    assert "bidplus.gem.gov.in" in settings.allowed_hosts


def test_score_weights_sum_to_100(settings):
    assert sum(settings.score_weights.values()) == 100


def test_score_bands_are_contiguous_and_cover_0_to_100(settings):
    bands = sorted(settings.score_bands, key=lambda b: b["min"])
    assert bands[0]["min"] == 0 and bands[-1]["max"] == 100
    for lower, upper in zip(bands, bands[1:], strict=False):
        assert upper["min"] == lower["max"] + 1


def test_urgency_bands_cover_the_whole_window(settings):
    bands = sorted(settings.urgency_bands, key=lambda b: b["min_days"])
    assert bands[0]["min_days"] == 0
    assert bands[-1]["max_days"] == settings.max_days_remaining
    for lower, upper in zip(bands, bands[1:], strict=False):
        assert upper["min_days"] == lower["max_days"] + 1


def test_search_queries_are_deduplicated(settings):
    queries = settings.search_queries()
    assert len(queries) == len({q.lower() for q in queries})
    assert len(queries) > 50


def test_every_taxonomy_group_has_queries_and_signals(settings):
    for name, group in settings.all_groups.items():
        assert group.get("queries"), f"{name} has no search queries"
        assert group.get("signals"), f"{name} has no match signals"
        assert group.get("label"), f"{name} has no label"


def test_company_profile_capabilities_reference_real_labels(settings):
    from gem_intel.analyze.profile import load_profile

    profile = load_profile(settings.config_dir / "company_profile.yaml")
    labels = {g["label"] for g in settings.all_groups.values()}
    for listed in profile.strong + profile.moderate + profile.weak:
        assert listed in labels, f"{listed!r} is not a taxonomy label"


@pytest.mark.parametrize("raw,expected", [
    ("14", 14), ("true", True), ("false", False), ("hello", "hello"),
])
def test_env_override_types(raw, expected):
    settings = load_settings(environ={"GEMINTEL_DEADLINE__MAX_DAYS_REMAINING": raw})
    assert settings.get("deadline.max_days_remaining") == expected


def test_env_override_creates_nested_paths():
    settings = load_settings(environ={"GEMINTEL_NEW__NESTED__VALUE": "7"})
    assert settings.get("new.nested.value") == 7


def test_missing_setting_raises():
    settings = load_settings(environ={})
    with pytest.raises(ConfigError):
        settings.require("nonexistent.setting")


def test_missing_config_directory_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nowhere")


def test_user_agent_carries_a_contact(monkeypatch, settings):
    monkeypatch.setenv("GEM_CONTACT_EMAIL", "ops@example.com")
    assert "ops@example.com" in settings.user_agent
    assert "{contact_email}" not in settings.user_agent
