"""Optional Redis leases and RunIdentity-driven run coordination."""

from __future__ import annotations

from ..errors import RedisLeaseError as RedisLeaseError
from ..errors import RedisLeaseLifecycleError as RedisLeaseLifecycleError
from ..errors import RedisLeaseProtocolError as RedisLeaseProtocolError
from ..errors import RedisLeaseTimeoutError as RedisLeaseTimeoutError
from ..errors import RedisLeaseUnavailableError as RedisLeaseUnavailableError

try:
    from ._backend import RedisRunCoordinator as RedisRunCoordinator
    from ._lease_lock import RedisLease as RedisLease
    from ._lease_lock import RedisLeaseLock as RedisLeaseLock
    from ._lease_lock import RedisLeaseLost as RedisLeaseLost
except ModuleNotFoundError as error:
    if error.name == "redis":
        raise ModuleNotFoundError(
            'Redis integration requires `pip install "tinkerfin[redis]"`.'
        ) from error
    raise

__all__ = [
    "RedisLease",
    "RedisLeaseError",
    "RedisLeaseLifecycleError",
    "RedisLeaseLock",
    "RedisLeaseLost",
    "RedisLeaseProtocolError",
    "RedisLeaseTimeoutError",
    "RedisLeaseUnavailableError",
    "RedisRunCoordinator",
]
