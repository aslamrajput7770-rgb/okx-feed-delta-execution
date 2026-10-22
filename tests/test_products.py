"""Tests for runtime product resolution.

The payloads below are trimmed copies of real venue responses, captured
2026-10-22. They are the evidence for the bug this module exists to prevent, so
they are kept verbatim rather than synthesised.

    PRODUCTION  id 27 -> BTCUSD,        perpetual, tick 0.5, cval 0.001
    TESTNET     id 27 -> BTCUSD_27Mar,  expired future, tick 0.5, cval 1.0
    TESTNET     id 84 -> BTCUSD,        perpetual, tick 0.1, cval 0.001
"""

from __future__ import annotations

import pytest

from orderflow import DELTA_BTCUSD
from orderflow.execution import ProductResolutionError, resolve_perpetual
from orderflow.execution.products import ResolvedProduct

PROD_BTCUSD = {
    "id": 27, "symbol": "BTCUSD", "contract_type": "perpetual_futures",
    "tick_size": "0.5", "contract_value": "0.001", "state": "live",
}
TESTNET_ID_27 = {
    "id": 27, "symbol": "BTCUSD_27Mar", "contract_type": "futures",
    "tick_size": "0.5", "contract_value": "1", "state": "expired",
}
TESTNET_PERP_84 = {
    "id": 84, "symbol": "BTCUSD", "contract_type": "perpetual_futures",
    "tick_size": "0.1", "contract_value": "0.001", "state": "live",
}
TESTNET_EXPIRED_PERP = {
    "id": 999, "symbol": "BTCUSD", "contract_type": "perpetual_futures",
    "tick_size": "0.5", "contract_value": "0.001", "state": "expired",
}


def fetch_returning(*rows):
    """A fetcher that returns the rows whose symbol matches the request."""
    def _fetch(base_url, symbol=None, timeout=10.0):
        return [r for r in rows if symbol is None or r["symbol"] == symbol]
    return _fetch


class TestProduction:
    def test_resolves_id_27(self):
        product, inst = resolve_perpetual(
            "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
            fetcher=fetch_returning(PROD_BTCUSD),
        )
        assert product.venue_id == 27
        assert inst.venue_id == 27
        assert inst.tick_size == pytest.approx(0.5)
        assert inst.contract_value == pytest.approx(0.001)

    def test_production_spec_matches_the_engine(self):
        product, _ = resolve_perpetual(
            "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
            fetcher=fetch_returning(PROD_BTCUSD),
        )
        assert product.diff_from(DELTA_BTCUSD) == []


class TestTestnetIdIsNotPortable:
    def test_the_testnet_perpetual_is_not_id_27(self):
        """The whole reason this module exists."""
        product, _ = resolve_perpetual(
            "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
            strict=False, fetcher=fetch_returning(TESTNET_ID_27, TESTNET_PERP_84),
        )
        assert product.venue_id == 84
        assert product.venue_id != 27

    def test_expired_dated_contract_is_never_chosen(self):
        """id 27 on testnet is an expired future with 1000x the contract value.

        It must not be selected even though its symbol shares the prefix.
        """
        product, _ = resolve_perpetual(
            "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
            strict=False, fetcher=fetch_returning(TESTNET_ID_27, TESTNET_PERP_84),
        )
        assert product.contract_type == "perpetual_futures"
        assert product.state == "live"
        assert product.contract_value == pytest.approx(0.001)

    def test_only_expired_perpetual_present_raises(self):
        with pytest.raises(ProductResolutionError) as exc:
            resolve_perpetual(
                "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(TESTNET_EXPIRED_PERP),
            )
        assert "no live perpetual" in str(exc.value)
        # The message must name the 1000x risk, since that is the consequence.
        assert "1000x" in str(exc.value) or "expired" in str(exc.value)

    def test_only_the_expired_dated_future_present_raises(self):
        with pytest.raises(ProductResolutionError):
            resolve_perpetual(
                "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(TESTNET_ID_27),
            )


class TestStrictSpecCheck:
    def test_tick_mismatch_does_not_block_resolution(self):
        """Testnet reports tick 0.1 where production reports 0.5.

        A tick difference moves the grid, but it does not change the order's
        size, so it must not stop a testnet run. `contract_value` is the
        money-critical field and is checked separately.
        """
        product, inst = resolve_perpetual(
            "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
            fetcher=fetch_returning(TESTNET_PERP_84),
        )
        assert inst.tick_size == pytest.approx(0.1)
        assert product.grid_problems(DELTA_BTCUSD) != []
        assert product.sizing_problems(DELTA_BTCUSD) == []

    def test_contract_value_mismatch_still_blocks_resolution(self):
        """The 1000x field: this one must stop everything."""
        bad = dict(PROD_BTCUSD, id=55, contract_value="1")
        with pytest.raises(ProductResolutionError) as exc:
            resolve_perpetual(
                "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(bad),
            )
        assert "1000x" in str(exc.value)

    def test_expired_state_still_blocks_resolution(self):
        """A dead contract must never be selected."""
        dead = dict(PROD_BTCUSD, state="expired")
        with pytest.raises(ProductResolutionError):
            resolve_perpetual(
                "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(dead),
            )

    def test_strict_mode_still_returns_the_venue_spec_on_mismatch(self):
        product, inst = resolve_perpetual(
            "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
            strict=False, fetcher=fetch_returning(TESTNET_PERP_84),
        )
        # The venue is the source of truth, so the instrument carries 0.1.
        assert inst.tick_size == pytest.approx(0.1)
        assert inst.contract_value == pytest.approx(0.001)

    def test_contract_value_mismatch_names_the_factor(self):
        """A 1000x size error must be reported as a factor, not just a diff."""
        bad = dict(PROD_BTCUSD, id=55, contract_value="1")
        with pytest.raises(ProductResolutionError) as exc:
            resolve_perpetual(
                "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(bad),
            )
        assert "1000x" in str(exc.value)

    def test_instrument_from_venue_spec_is_usable_for_sizing(self):
        from orderflow.execution import DELTA_PERP_RULES, contracts_for_base

        _, inst = resolve_perpetual(
            "https://cdn-ind.testnet.deltaex.org", "BTCUSD", DELTA_BTCUSD,
            strict=False, fetcher=fetch_returning(TESTNET_PERP_84),
        )
        order = contracts_for_base(inst, 0.01, DELTA_PERP_RULES)
        assert order.contracts == pytest.approx(10.0)
        assert order.base == pytest.approx(0.01)


class TestResolutionEdges:
    def test_unknown_symbol_raises_with_the_symbol_named(self):
        with pytest.raises(ProductResolutionError) as exc:
            resolve_perpetual(
                "https://api.india.delta.exchange", "NOPEUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(PROD_BTCUSD),
            )
        assert "NOPEUSD" in str(exc.value)

    def test_two_live_perpetuals_is_ambiguous_and_raises(self):
        """Never let the first of several candidates win silently."""
        second = dict(PROD_BTCUSD, id=28)
        with pytest.raises(ProductResolutionError) as exc:
            resolve_perpetual(
                "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(PROD_BTCUSD, second),
            )
        assert "ambiguous" in str(exc.value)

    def test_a_perfectly_matching_duplicate_pair_still_raises(self):
        """Ambiguity is about identity, not about spec differences."""
        twin = dict(PROD_BTCUSD, id=29)
        with pytest.raises(ProductResolutionError):
            resolve_perpetual(
                "https://api.india.delta.exchange", "BTCUSD", DELTA_BTCUSD,
                fetcher=fetch_returning(PROD_BTCUSD, twin),
            )

    def test_diff_from_reports_every_problem(self):
        product = ResolvedProduct(
            venue_id=1, symbol="X", contract_type="something_new",
            tick_size=0.1, contract_value=1.0, state="expired",
        )
        problems = product.diff_from(DELTA_BTCUSD)
        joined = " ".join(problems)
        assert "state" in joined
        assert "contract_type" in joined
        assert "tick_size" in joined  # a grid problem, not a sizing one
        assert "contract_value" in joined
        assert len(problems) == 4

    def test_diff_from_accepts_dated_and_spot_types(self):
        """`futures` and `spot` are legitimate types; only unknown ones flag."""
        for ctype in ("perpetual_futures", "futures", "spot"):
            product = ResolvedProduct(
                venue_id=1, symbol="X", contract_type=ctype,
                tick_size=DELTA_BTCUSD.tick_size,
                contract_value=DELTA_BTCUSD.contract_value, state="live",
            )
            assert product.diff_from(DELTA_BTCUSD) == []
