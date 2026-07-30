from subbench.weights import MIN_WINDOW_MARGIN, solve


def window(provider="codex", quota=0.0, **tokens):
    return {"provider": provider, "quota_percent": quota, "tokens": tokens}


def test_recovers_known_weights():
    # terra costs 2 quota points per million tokens, sol costs 6.
    truth = {"terra": 2e-6, "sol": 6e-6}
    windows = []
    for terra, sol in [(9e6, 1e6), (1e6, 9e6), (5e6, 5e6), (7e6, 3e6), (2e6, 8e6), (6e6, 4e6)]:
        quota = terra * truth["terra"] + sol * truth["sol"]
        windows.append(window(quota=quota, terra=terra, sol=sol))
    fit = solve(windows, provider="codex")
    assert fit.sufficient, fit.reason
    recovered = dict(zip(fit.models, fit.weights))
    assert abs(recovered["terra"] - truth["terra"]) < 2e-7
    assert abs(recovered["sol"] - truth["sol"]) < 2e-7
    # Residual is in quota-percent units against targets of order 20-60, so judge it
    # relative to the data rather than against an absolute floor an iterative solver
    # will not reach.
    assert fit.residual / sum(row["quota_percent"] for row in windows) < 1e-4


def test_too_few_windows_refuses_to_guess():
    # The failure this guards: least squares happily returns an authoritative-looking
    # answer for an underdetermined system.
    windows = [window(quota=10.0, terra=1e6, sol=1e6, luna=1e6, gpt55=1e6)]
    fit = solve(windows, provider="codex")
    assert not fit.sufficient
    assert fit.weights == []
    assert "need" in fit.reason


def test_required_window_count_scales_with_model_count():
    windows = [window(quota=float(i + 1), terra=1e6 * (i + 1), sol=1e6) for i in range(4)]
    fit = solve(windows, provider="codex")
    assert not fit.sufficient
    assert str(2 + MIN_WINDOW_MARGIN) in fit.reason


def test_identical_mix_across_windows_is_not_separable():
    # Same proportions every window: the models move together, so no amount of data
    # separates their contributions.
    windows = [window(quota=float(i + 1), terra=5e6 * (i + 1), sol=5e6 * (i + 1)) for i in range(8)]
    fit = solve(windows, provider="codex")
    assert not fit.sufficient
    assert "varies by less than" in fit.reason


def test_weights_are_never_negative():
    # A negative weight would mean a model refunded quota.
    windows = []
    for terra, sol in [(9e6, 1e6), (1e6, 9e6), (5e6, 5e6), (8e6, 2e6), (3e6, 7e6), (6e6, 4e6)]:
        windows.append(window(quota=terra * 3e-6, terra=terra, sol=sol))
    fit = solve(windows, provider="codex")
    assert fit.sufficient
    assert all(weight >= 0.0 for weight in fit.weights)


def test_providers_are_fitted_separately():
    windows = [window(provider="claude", quota=1.0, opus=1e6) for _ in range(9)]
    fit = solve(windows, provider="codex")
    assert not fit.sufficient
    assert fit.reason == "no model token counts recorded"


def test_no_observations_is_reported_not_crashed():
    fit = solve([], provider="codex")
    assert not fit.sufficient
    assert fit.models == []
