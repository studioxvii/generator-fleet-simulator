from tools.launch_feature_gate import (
    FEATURES,
    feature_catalog,
    selected_features,
    utility_fail_observed,
    utility_restore_observed,
)


def test_launch_feature_catalog_covers_the_overnight_matrix():
    ids = [item.id for item in FEATURES]
    assert ids == [f"F{index:02d}" for index in range(1, 22)]
    catalog = feature_catalog()
    assert {row["id"] for row in catalog} == set(ids)
    titles = {row["id"]: row["title"] for row in catalog}
    assert "SCADA" in titles["F03"] or "Socket.IO" in titles["F03"]
    assert "SCADA" in titles["F13"]
    assert "2,000" in titles["F21"]


def test_selected_features_keeps_shutdown_last_and_skips_p4_on_p2():
    p2_ids = [item.id for item in selected_features("", "p2")]
    assert "F21" not in p2_ids
    assert p2_ids[-1] == "F20"
    p4_ids = [item.id for item in selected_features("", "p4")]
    assert p4_ids[-1] == "F20"
    assert "F21" in p4_ids
    only = [item.id for item in selected_features("F21,F06,F20", "p2")]
    assert only == ["F06", "F21", "F20"]


def test_utility_fail_predicate_rejects_running_without_open_breaker_or_phase_loss():
    payload = {
        "generator": {
            "state": "RUNNING",
            "utility_breaker": True,
            "gen_breaker": False,
            "alarms": {},
        }
    }
    assert utility_fail_observed(payload) is False


def test_utility_fail_predicate_requires_open_breaker_and_phase_loss():
    running_only = {
        "generator": {
            "state": "CRANKING",
            "utility_breaker": True,
            "alarms": {"27": "Utility Phase Loss"},
        }
    }
    assert utility_fail_observed(running_only) is False
    failed = {
        "generator": {
            "state": "RUNNING",
            "utility_breaker": False,
            "alarms": {"27": "Utility Phase Loss"},
        }
    }
    assert utility_fail_observed(failed) is True


def test_utility_restore_predicate_clears_phase_loss_and_closes_utility_when_gen_open():
    still_failed = {
        "generator": {
            "utility_breaker": False,
            "gen_breaker": False,
            "alarms": {"27": "Utility Phase Loss"},
        }
    }
    assert utility_restore_observed(still_failed) is False
    island_after_restore = {
        "generator": {
            "utility_breaker": False,
            "gen_breaker": True,
            "alarms": {},
        }
    }
    assert utility_restore_observed(island_after_restore) is True
    transfer_after_restore = {
        "generator": {
            "utility_breaker": True,
            "gen_breaker": False,
            "alarms": {},
        }
    }
    assert utility_restore_observed(transfer_after_restore) is True
    gen_open_utility_still_open = {
        "generator": {
            "utility_breaker": False,
            "gen_breaker": False,
            "alarms": {},
        }
    }
    assert utility_restore_observed(gen_open_utility_still_open) is False
