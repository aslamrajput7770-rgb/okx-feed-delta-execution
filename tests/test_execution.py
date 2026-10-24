"""Tests for position sizing and the Delta client. No network, no mocks of real
code paths: the HTTP layer is replaced by an injected sender, which is a seam
the production code itself provides.
"""

from __future__ import annotations

import json

import pytest

from orderflow import DELTA_BTCUSD, build_default_map
from orderflow.execution import (
    DELTA_PERP_RULES,
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
class TestPositionSizing:
    def test_one_contract_is_not_one_coin(self):
        """The whole point: 1 BTC is 1000 contracts, not 1."""
        delta = contracts_for_base(DELTA_BTCUSD, 1.0, DELTA_PERP_RULES)
        assert delta.contracts == pytest.approx(1000.0)
        # Sending "1" for a 1-BTC intention would be short by 1000x.
        assert delta.contracts != 1.0

    def test_the_round_trip_preserves_the_exposure(self):
        order = contracts_for_base(DELTA_BTCUSD, 0.5, DELTA_PERP_RULES)
        assert order.base == pytest.approx(0.5)

    def test_delta_lot_step_is_respected(self):
        """Delta takes whole contracts, so a finer exposure floors down."""
        # 0.01535 / 0.001 = 15.35 contracts -> floors to 15
        order = contracts_for_base(DELTA_BTCUSD, 0.01535, DELTA_PERP_RULES)
        assert order.contracts == pytest.approx(15.0)
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
        """`0.1 // 0.001` is 99.0 on floats; Decimal must give 100 lots."""
        order = contracts_for_base(DELTA_BTCUSD, 0.1, DELTA_PERP_RULES)
        assert order.contracts == pytest.approx(100.0)

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

    def test_server_time_parses_the_date_header(self):
        """Delta has no /v2/time; the clock comes from the Date header.

        The parse is the real one: only the header fetch is injected.
        """
        client = DeltaOrderClient(
            head_sender=lambda url, headers: "Thu, 22 Oct 2026 22:22:18 GMT"
        )
        # 2026-10-22T22:22:18Z, independently confirmed via datetime
        assert client.server_time() == pytest.approx(1792707738.0)

    def test_missing_date_header_raises(self):
        client = DeltaOrderClient(head_sender=lambda url, headers: None)
        with pytest.raises(OrderError):
            client.server_time()

    def test_probe_targets_the_v2_products_endpoint(self):
        """The endpoint that actually exists: /v2/time 404s on both hosts."""
        seen = {}

        def head(url, headers):
            seen["url"] = url
            seen["headers"] = headers
            return "Thu, 22 Oct 2026 22:22:18 GMT"

        DeltaOrderClient(head_sender=head).server_time()
        assert "/v2/products" in seen["url"]
        assert "delta" in seen["url"]

    def test_probe_sends_a_range_get_not_a_head(self):
        """HEAD 404s on the live host and intermittently on testnet.

        A one-byte Range GET returns 206 with the headers intact, so the clock
        must not depend on HEAD.
        """
        seen = {}

        def head(url, headers):
            seen.update(headers)
            return "Thu, 22 Oct 2026 22:22:18 GMT"

        DeltaOrderClient(head_sender=head).server_time()
        assert seen.get("Range") == "bytes=0-0"

    def test_probe_url_is_unique_so_the_cdn_cannot_replay_a_cached_date(self):
        """The measured failure: cache-control max-age=60 replays a stale Date.

        A response with `Age: 39` reported a ~40s skew on an accurate clock.
        `Cache-Control: no-cache` did not help, so each probe must be a distinct
        URL. Two probes in the same millisecond must still differ.
        """
        urls = []

        def head(url, headers):
            urls.append(url)
            return "Thu, 22 Oct 2026 22:22:18 GMT"

        client = DeltaOrderClient(clock=lambda: 100.0, head_sender=head)
        client.server_time()
        client.server_time()
        assert len(urls) == 2
        assert urls[0] != urls[1], "identical probe URLs can be served from cache"
        assert all("_probe=" in u for u in urls)

    def test_probe_url_keeps_the_existing_query_string(self):
        """A cache-buster must append, not replace, an existing query string."""
        seen = {}

        def head(url, headers):
            seen["url"] = url
            return "Thu, 22 Oct 2026 22:22:18 GMT"

        client = DeltaOrderClient(head_sender=head)
        client._venue_date_header("https://x/v2/products?a=1")  # noqa: SLF001
        assert "a=1" in seen["url"]
        assert "&_probe=" in seen["url"]


# ------------------------------------------------------------------- wiring
class TestWiring:
    def test_default_map_holds_the_delta_perpetual(self):
        m = build_default_map()
        assert len(m) == 1
        assert m.by_symbol(  # type: ignore[attr-defined]
            __import__("orderflow").Exchange.DELTA, "BTCUSD"
        ).venue_id == 27

    def test_sole_resolves_the_one_instrument(self):
        m = build_default_map()
        assert m.sole("BTCUSD") is DELTA_BTCUSD

    def test_sole_raises_on_an_unknown_symbol(self):
        from orderflow import UnknownSymbol

        with pytest.raises(UnknownSymbol):
            build_default_map().sole("NOT-A-SYMBOL")

    def test_registering_the_same_key_twice_is_refused(self):
        """A silent overwrite would repoint sizing at a different contract."""
        from orderflow import Exchange, Instrument, InstrumentMap, Segment

        m = InstrumentMap()
        m.register(Instrument("X", Exchange.DELTA, Segment.SWAP, 0.5, 0.001))
        with pytest.raises(ValueError, match="already maps to"):
            m.register(Instrument("X", Exchange.DELTA, Segment.FUT, 0.5, 0.5))
