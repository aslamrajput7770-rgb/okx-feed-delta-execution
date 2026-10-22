"""Execution: sizing across venues, and the Delta India order client.

    sizing       : contract arithmetic between OKX and Delta
    delta_client : signed REST orders, dry-run and testnet by default

Credentials are read from the environment (`DELTA_API_KEY`,
`DELTA_API_SECRET`) and are never accepted as literals.
"""

from .delta_client import (
    BASE_URLS,
    ENV_API_KEY,
    ENV_API_SECRET,
    MAX_CLOCK_SKEW_SECONDS,
    SIGNATURE_TTL_SECONDS,
    DeltaCredentials,
    DeltaOrderClient,
    Environment,
    LiveOrderRefused,
    MissingCredentials,
    OrderError,
    OrderRequest,
    OrderResult,
    delta_instrument,
    generate_signature,
    signature_payload,
)
from .sizing import (
    DELTA_PERP_RULES,
    OKX_SWAP_RULES,
    SizedOrder,
    SizingError,
    VenueRules,
    contracts_for_base,
    rebalance,
)

__all__ = [
    # sizing
    "VenueRules",
    "OKX_SWAP_RULES",
    "DELTA_PERP_RULES",
    "SizedOrder",
    "SizingError",
    "contracts_for_base",
    "rebalance",
    # delta client
    "Environment",
    "BASE_URLS",
    "SIGNATURE_TTL_SECONDS",
    "MAX_CLOCK_SKEW_SECONDS",
    "ENV_API_KEY",
    "ENV_API_SECRET",
    "DeltaCredentials",
    "OrderRequest",
    "OrderResult",
    "OrderError",
    "MissingCredentials",
    "LiveOrderRefused",
    "DeltaOrderClient",
    "delta_instrument",
    "generate_signature",
    "signature_payload",
]
