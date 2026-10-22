"""Tests for cross-venue sizing and the Delta client. No network, no mocks of
real code paths: the HTTP layer is replaced by an injected sender, which is a
seam the production code itself provides.
"""

from __future__ import annotations

import json

import pytest

from orderflow import DELTA_BTCUSD, OKX_BTC_USDT_SWAP, build_default_map
from orderflow.execution import (
    DELTA_PERP_RULES,
    OKX_SWAP_RULES,
    DeltaCredentials,
    DeltaOrderClient,
    Environment,
    LiveOrderRefused,
    MissingCredentials,
    OrderError,
    OrderRequest,
    SizingError,
    contracts_for_base,
    generate_signature,
    rebalance,
    signature_payload,
)


# ------------------------------------------------------------------- sizing
class TestCrossVenueSizing:
    def test_the_two_contracts_are_not_interchangeable(self):
        """The whole point: 1 BTC is 100 contracts on OKX and 1000 on Delta."""
        okx = contracts_for_base(OKX_BTC_USDT_SWAP, 1.0, OKX_SWAP_RULES)
        delta = contracts_for_base(DELTA_BTCUSD, 1.0, DELTA_PERP_RULES)

        assert okx.contracts == pytest.approx(100.0)
        assert delta.contracts == pytest.approx(1000.0)
        # Copying the number across venues would be out by 10x.
        assert okx.contracts != delta.contracts
        assert delta.contracts / okx.contracts == pytest.approx(10.0)

    def test_both_sides_represent_the_same_exposure(self):
        okx, delta = rebalance(
            OKX_BTC_USDT_SWAP, DELTA_BTCUSD, 0.5, OKX_SWAP_RULES, DELTA_PERP_RULES
        )
        assert okx.base == pytest.approx(0.5)
        assert delta.base == pytest.approx(0.5)

    def test_okx_lot_step_is_respected(self):
        """OKX accepts 0.01-contract steps, so a finer exposure floors to one."""
        # 0.01535 / 0.01 = 1.535 contracts -> floors to 1.53
        order = contracts_for_base(OKX_BTC_USDT_SWAP, 0.01535, OKX_SWAP_RULES)
        assert order.contracts == pytest.approx(1.53)
        assert order.rounded is True

    def test_delta_floors_a_sub_contract_exposure_to_zero(self):
        """0.0005 base is half a Delta contract, and half a contract is nothing."""
        with pytest.raises(SizingError):
            contracts_for_base(DELTA_BTCUSD, 0.0005, DELTA_PERP_RULES)

    def test_rounding_is_down_not_nearest(self):
        # 0.0019 base = 1.9 contracts -> 1, not 2.
        order = contracts_for_base(DELTA_BTCUSD, 0.0019, DELTA_PERP_RULES)
        assert order.contracts == pytest.approx(1.0)
        assert order.base == pytest.approx(0.001)

    def test_below_minimum_raises_rather_than_silently_no_op(self):
        with pytest.raises(SizingError) as exc:
            contracts_for_base(DELTA_BTCUSD, 0.0001, DELTA_PERP_RULES)
        assert "below the venue minimum" in str(exc.value)

    def test_zero_and_negative_rejected(self):
        with pytest.raises(SizingError):
            contracts_for_base(DELTA_BTCUSD, 0.0, DELTA_PERP_RULES)
        with pytest.raises(SizingError):
            contracts_for_base(DELTA_BTCUSD, -1.0, DELTA_PERP_RULES)

    def test_decimal_arithmetic_does_not_lose_a_lot(self):
        """`0.1 // 0.01` is 9.0 on floats; Decimal must give 10 lots."""
        order = contracts_for_base(OKX_BTC_USDT_SWAP, 0.1, OKX_SWAP_RULES)
        assert order.contracts == pytest.approx(10.0)

    def test_exact_exposure_is_not_flagged_rounded(self):
        order = contracts_for_base(DELTA_BTCUSD, 0.01, DELTA_PERP_RULES)
        assert order.contracts == pytest.approx(10.0)
        assert order.rounded is False


# ------------------------------------------------------------------ signing
class TestSignature:
    def test_signature_is_hmac_sha256_hex(self):
        sig = generate_signature("secret", "GET123/v2/profile")
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)

    def test_payload_component_order(self):
        """method + timestamp + path + query + body, empty parts included."""
        assert signature_payload("POST", "123", "/v2/orders", "", "{}") == "POST123/v2/orders{}"
        assert signature_payload("get", "1", "/p", "a=1", "") == "GET1/pa=1"

    def test_empty_query_string_is_still_a_component(self):
        """Dropping it breaks every GET signature; keep it explicit."""
        assert signature_payload("GET", "1", "/v2/orders") == "GET1/v2/orders"

    def test_signature_is_deterministic(self):
        a = generate_signature("s", "GET1/x")
        b = generate_signature("s", "GET1/x")
        assert a == b

    def test_different_secrets_differ(self):
        assert generate_signature("a", "x") != generate_signature("b", "x")


# ---------------------------------------------------------------- credentials
class TestCredentials:
    def test_from_env_reads_both(self):
        creds = DeltaCredentials.from_env(
            {"DELTA_API_KEY": "k", "DELTA_API_SECRET": "s"}
        )
        assert creds.api_key == "k"

    def test_missing_secret_raises(self):
        with pytest.raises(MissingCredentials) as exc:
            DeltaCredentials.from_env({"DELTA_API_KEY": "k"})
        assert "DELTA_API_SECRET" in str(exc.value)

    def test_missing_key_raises(self):
        with pytest.raises(MissingCredentials):
            DeltaCredentials.from_env({})

    def test_repr_masks_the_secret(self):
        """A secret that reaches a log line has leaked."""
        creds = DeltaCredentials.from_env(
            {"DELTA_API_KEY": "abcdef123", "DELTA_API_SECRET": "supersecret"}
        )
        text = repr(creds)
        assert "supersecret" not in text
        assert "<hidden>" in text


# ------------------------------------------------------------------- guards
class TestSafetyGuards:
    def test_dry_run_is_the_default(self):
        client = DeltaOrderClient()
        assert client.dry_run is True
        result = client.buy(product_id=27, size=1)
        assert result.sent is False
        assert result.ok is True

    def test_testnet_is_the_default(self):
        assert DeltaOrderClient().environment is Environment.TESTNET

    def test_live_order_without_opt_in_is_refused(self):
        """Two flags must be set the wrong way before real money moves."""
        client = DeltaOrderClient(
            credentials=DeltaCredentials.from_env({"DELTA_API_KEY": "k", "DELTA_API_SECRET": "s"}),
            environment=Environment.LIVE,
            allow_live=False,
            dry_run=False,
        )
        with pytest.raises(LiveOrderRefused):
            client.buy(product_id=27, size=1)

    def test_dry_run_needs_no_credentials(self):
        """A dry run on a laptop with no keys is legitimate."""
        client = DeltaOrderClient(dry_run=True)
        assert client.credentials is None
        assert client.buy(product_id=27, size=1).ok is True

    def test_non_dry_run_without_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("DELTA_API_KEY", raising=False)
        monkeypatch.delenv("DELTA_API_SECRET", raising=False)
        with pytest.raises(MissingCredentials):
            DeltaOrderClient(dry_run=False)

    def test_testnet_order_needs_allow_live_but_not_mainnet_flag(self):
        """Testnet is a real order, so it needs the network flag."""
        client = DeltaOrderClient(
            credentials=DeltaCredentials.from_env({"DELTA_API_KEY": "k", "DELTA_API_SECRET": "s"}),
            environment=Environment.TESTNET,
            allow_live=True,
            dry_run=False,
            sender=lambda *a: (200, {"result": {"id": 1}}),
        )
        result = client.buy(product_id=27, size=1)
        assert result.sent is True
        assert result.status_code == 200
        assert result.ok is True

    def test_base_urls_are_the_india_hosts(self):
        """India keys are rejected by the global host; the URLs must be India."""
        assert "india" in DeltaOrderClient(environment=Environment.LIVE).base_url
        assert "ind.testnet" in DeltaOrderClient(environment=Environment.TESTNET).base_url


# ------------------------------------------------------------------ payload
class TestOrderRequest:
    def test_market_order_payload(self):
        req = OrderRequest(product_id=27, size=5, side="buy")
        assert req.to_payload() == {
            "product_id": 27, "size": 5, "side": "buy", "order_type": "market_order",
        }

    def test_canonical_body_has_no_spaces(self):
        """The signed string and the sent string must be byte-identical."""
        req = OrderRequest(product_id=27, size=5, side="buy")
        body = req.canonical_body()
        assert " " not in body
        assert json.loads(body) == req.to_payload()

    def test_limit_order_requires_price(self):
        with pytest.raises(OrderError):
            OrderRequest(product_id=27, size=1, side="buy", order_type="limit_order")

    def test_limit_order_carries_price(self):
        req = OrderRequest(
            product_id=27, size=1, side="sell", order_type="limit_order", limit_price="90000.5"
        )
        assert req.to_payload()["limit_price"] == "90000.5"

    def test_fractional_size_rejected(self):
        """Delta sizes are whole contracts; a fraction is an upstream bug."""
        with pytest.raises(OrderError):
            OrderRequest(product_id=27, size=1.5, side="buy")

    def test_bad_side_rejected(self):
        with pytest.raises(OrderError):
            OrderRequest(product_id=27, size=1, side="long")

    def test_reduce_only_flag(self):
        req = OrderRequest(product_id=27, size=1, side="sell", reduce_only=True)
        assert req.to_payload()["reduce_only"] is True


# --------------------------------------------------------------- transport
class TestTransportPath:
    def _client(self, sender, **kw):
        return DeltaOrderClient(
            credentials=DeltaCredentials.from_env({"DELTA_API_KEY": "k", "DELTA_API_SECRET": "s"}),
            environment=Environment.TESTNET,
            allow_live=True,
            dry_run=False,
            sender=sender,
            **kw,
        )

    def test_headers_are_sent(self):
        seen = {}

        def sender(method, url, headers, body):
            seen.update({"method": method, "url": url, "headers": headers, "body": body})
            return 200, {"result": {"id": 1}}

        self._client(sender).buy(product_id=27, size=1)
        assert seen["method"] == "POST"
        assert seen["url"].endswith("/v2/orders")
        for h in ("api-key", "signature", "timestamp", "Content-Type"):
            assert h in seen["headers"]
        assert len(seen["headers"]["signature"]) == 64

    def test_signature_matches_the_documented_algorithm(self):
        """Recompute the signature from the sent body and compare."""
        seen = {}

        def sender(method, url, headers, body):
            seen.update({"headers": headers, "body": body})
            return 200, {}

        client = self._client(sender, clock=lambda: 1700000000.0)
        client.buy(product_id=27, size=1)

        expected = generate_signature(
            "s", "POST1700000000/v2/orders" + seen["body"]
        )
        assert seen["headers"]["signature"] == expected

    def test_http_error_is_reported_not_raised(self):
        import urllib.error

        def sender(method, url, headers, body):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        result = self._client(sender).buy(product_id=27, size=1)
        assert result.ok is False
        assert result.status_code == 401

    def test_network_error_is_reported_not_raised(self):
        import urllib.error

        def sender(method, url, headers, body):
            raise urllib.error.URLError("no route to host")

        result = self._client(sender).buy(product_id=27, size=1)
        assert result.ok is False
        assert "URLError" in result.error

    def test_non_200_is_not_ok(self):
        result = self._client(lambda *a: (400, {"error": "bad"})).buy(product_id=27, size=1)
        assert result.ok is False
        assert result.status_code == 400


# ---------------------------------------------------------------- clock skew
class TestClockSkew:
    def test_skew_within_limit_passes(self):
        client = DeltaOrderClient(clock=lambda: 100.0)
        client.server_time = lambda: 99.5  # type: ignore[method-assign]
        client.check_clock()  # must not raise

    def test_skew_beyond_limit_raises_with_ntp_hint(self):
        """A skewed clock rejects every order; the message must say why."""
        client = DeltaOrderClient(clock=lambda: 100.0)
        client.server_time = lambda: 90.0  # type: ignore[method-assign]
        with pytest.raises(OrderError) as exc:
            client.check_clock()
        assert "ntp" in str(exc.value).lower() or "chrony" in str(exc.value).lower()

    def test_skew_sign_is_ours_minus_theirs(self):
        client = DeltaOrderClient(clock=lambda: 105.0)
        client.server_time = lambda: 100.0  # type: ignore[method-assign]
        assert client.clock_skew() == pytest.approx(5.0)


# ------------------------------------------------------------------- wiring
class TestWiring:
    def test_default_map_holds_both_legs(self):
        m = build_default_map()
        assert len(m) == 2
        assert m.by_symbol(  # type: ignore[attr-defined]
            __import__("orderflow").Exchange.OKX, "BTC-USDT-SWAP"
        ).contract_value == pytest.approx(0.01)
        assert m.by_symbol(  # type: ignore[attr-defined]
            __import__("orderflow").Exchange.DELTA, "BTCUSD"
        ).venue_id == 27

    def test_sole_raises_on_ambiguous_symbol(self):
        from orderflow import Exchange, Instrument, InstrumentMap, Segment, UnknownSymbol

        m = InstrumentMap()
        m.register(Instrument("X", Exchange.OKX, Segment.SWAP, 0.1, 0.01))
        m.register(Instrument("X", Exchange.DELTA, Segment.SWAP, 0.5, 0.001))
        with pytest.raises(UnknownSymbol):
            m.sole("X")
