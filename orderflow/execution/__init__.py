"""Execution: sizing, and the Delta India order client.

    sizing       : contract arithmetic for Delta
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
from .products import (
    ProductResolutionError,
    ResolvedProduct,
    fetch_products,
    resolve_perpetual,
)
from .risk import (
    ExitReason,
    ExitSignal,
    Position,
    RiskDecision,
    RiskLimits,
    RiskManager,
    RiskState,
)
from .sizing import (
    DELTA_PERP_RULES,
    SizedOrder,
    SizingError,
    VenueRules,
    contracts_for_base,
    rebalance,
)

__all__ = [
    # sizing
    "VenueRules",
    "DELTA_PERP_RULES",
    "SizedOrder",
    "SizingError",
    "contracts_for_base",
    "rebalance",
    # risk
    "RiskLimits",
    "RiskDecision",
    "Position",
    "RiskManager",
    "RiskState",
    "ExitSignal",
    "ExitReason",
    # products
    "ResolvedProduct",
    "ProductResolutionError",
    "fetch_products",
    "resolve_perpetual",
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
