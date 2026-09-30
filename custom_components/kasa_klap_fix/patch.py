"""Work around KLAP authentication failures on legacy Kasa devices.

Two independent fallbacks, applied to python-kasa at runtime.

1. force_xor_hosts
   Several Kasa models that HA discovers as KLAP still answer the legacy
   unauthenticated XOR protocol on port 9999. `kasa/device_factory.py` maps the
   whole IOT family to KLAP:

       "IOT.KLAP": (IotProtocol, KlapTransport),

   ...because discovery on port 20002 advertises KLAP, even though port 9999 is
   open and working. For the hosts listed here we return the XOR transport
   instead, so no credentials are needed at all.

2. KLAP v1 -> v2 auto-fallback
   Legacy devices that take a firmware update keep the IOT command set but move
   to KLAP v2 hashing:

       v1: md5(md5(user) + md5(pass))
       v2: sha256(sha1(user) + sha1(pass))

   They are hashed with v1 and fail with "Device response did not match our
   challenge". python-kasa PR #1731 only fixes this when the device reports
   `mgt_encrypt_schm.lv >= 2`; devices signalling with `new_klap: 1` are missed,
   because `EncryptionScheme` has no `new_klap` field and the flag is dropped
   during parsing. So we ignore the advertised flags and retry on failure.

A device that works on v1 never takes an extra round trip, which keeps this safe
on a mixed fleet.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

# python-kasa classes, resolved lazily by _load_kasa() rather than at module
# import time. Importing kasa submodules does blocking filesystem work (a
# `listdir` over site-packages), and this module is first imported on Home
# Assistant's event loop. Deferring the imports into _load_kasa() — which runs
# from an executor via apply() — keeps that work off the loop and clears HA's
# "Detected blocking call to listdir" warning.
device_factory = None
DeviceConfig = None
AuthenticationError = None
IotProtocol = None
KlapTransport = None
KlapTransportV2 = None
XorTransport = None


def _load_kasa() -> None:
    """Import python-kasa classes into module globals.

    Called from apply(), which runs in an executor, so the blocking import work
    stays off the event loop. python-kasa has moved these between modules across
    releases, so each is resolved from its known locations in turn.
    """
    global device_factory, DeviceConfig, AuthenticationError
    global IotProtocol, KlapTransport, KlapTransportV2, XorTransport

    from kasa import device_factory as _device_factory
    from kasa.deviceconfig import DeviceConfig as _DeviceConfig
    from kasa.exceptions import AuthenticationError as _AuthenticationError

    device_factory = _device_factory
    DeviceConfig = _DeviceConfig
    AuthenticationError = _AuthenticationError

    for _mod in ("kasa.protocols", "kasa.iotprotocol", "kasa"):
        try:
            IotProtocol = __import__(_mod, fromlist=["IotProtocol"]).IotProtocol
            break
        except (ImportError, AttributeError):
            continue

    for _mod in ("kasa.transports", "kasa.klaptransport", "kasa"):
        try:
            _m = __import__(_mod, fromlist=["KlapTransport"])
            KlapTransport = _m.KlapTransport
            KlapTransportV2 = _m.KlapTransportV2
            break
        except (ImportError, AttributeError):
            continue

    for _mod in ("kasa.transports", "kasa.xortransport", "kasa.protocol", "kasa"):
        try:
            XorTransport = __import__(_mod, fromlist=["XorTransport"]).XorTransport
            break
        except (ImportError, AttributeError):
            continue

_PATCH_FLAG = "_kasa_klap_fix_patched"

#: Hosts forced onto XOR immediately, skipping the KLAP attempt entirely.
#: Optional - the automatic fallback below handles this without a list, so
#: only use it to avoid the one failed handshake per device at startup.
FORCE_XOR_HOSTS: set[str] = set()


def _rehash(transport: KlapTransport) -> None:
    """Recompute cached auth hashes after the transport's class has changed."""
    if transport._credentials:
        transport._local_auth_hash = transport.generate_auth_hash(transport._credentials)
        transport._local_auth_owner = transport.generate_owner_hash(
            transport._credentials
        ).hex()
    transport._default_credentials_auth_hash = {}
    transport._blank_auth_hash = None


def _patch_handshake() -> None:
    original = KlapTransport.perform_handshake1

    async def perform_handshake1(self):  # type: ignore[no-untyped-def]
        try:
            return await original(self)
        except AuthenticationError:
            # Already v2: credentials really are wrong, or this device uses a
            # derivation nobody has worked out (python-kasa #1752 / #1754).
            if isinstance(self, KlapTransportV2):
                raise
            _LOGGER.debug("KLAP v1 failed for %s, retrying with v2", self._host)
            self.__class__ = KlapTransportV2
            _rehash(self)
            try:
                result = await original(self)
            except AuthenticationError:
                self.__class__ = KlapTransport  # put it back
                _rehash(self)
                raise
            _LOGGER.warning(
                "Device %s authenticated with KLAP v2 hashes despite being an "
                "IOT-family device; using KlapTransportV2 from now on.",
                self._host,
            )
            return result

    KlapTransport.perform_handshake1 = perform_handshake1  # type: ignore[method-assign]


#: Hosts already proven to speak XOR, so a transient refusal on port 9999
#: cannot demote a device that was working a minute ago.
_XOR_PROVEN: set[str] = set()


async def _xor_reachable(host: str, timeout: float = 4.0, attempts: int = 3) -> bool:
    """True if the device answers the legacy XOR port.

    These devices accept only one connection at a time, so a single refused
    connect means "busy", not "unsupported". Retry before concluding anything.
    """
    if host in _XOR_PROVEN:
        return True
    for attempt in range(attempts):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, 9999), timeout
            )
            writer.close()
            _XOR_PROVEN.add(host)
            return True
        except Exception:
            if attempt < attempts - 1:
                await asyncio.sleep(2)
    return False


def _patch_iot_query() -> None:
    """Fall back to unauthenticated XOR when KLAP auth fails.

    Keyed on nothing but the live connection, so it follows devices across DHCP
    address changes instead of relying on a hand-maintained IP list.
    """
    original = IotProtocol.query

    async def query(self, request, retry_count: int = 3):  # type: ignore[no-untyped-def]
        try:
            return await original(self, request, retry_count)
        except AuthenticationError:
            host = self._transport._host
            if isinstance(self._transport, XorTransport):
                raise
            if not await _xor_reachable(host):
                raise  # no legacy port; nothing to fall back to
            _LOGGER.warning(
                "KLAP authentication failed for %s but port 9999 is open; "
                "falling back to the unauthenticated XOR transport.",
                host,
            )
            config = self._transport._config
            try:
                await self._transport.close()
            except Exception:
                pass
            self._transport = XorTransport(config=config)
            return await original(self, request, retry_count)

    IotProtocol.query = query  # type: ignore[method-assign]


def _patch_factory() -> None:
    original = device_factory.get_protocol

    def get_protocol(config: DeviceConfig, *, strict: bool = False):
        if config.host in FORCE_XOR_HOSTS:
            _LOGGER.info(
                "Forcing unauthenticated XOR transport for %s", config.host
            )
            return IotProtocol(transport=XorTransport(config=config))
        return original(config, strict=strict)

    device_factory.get_protocol = get_protocol
    # The tplink integration imports the symbol directly in some versions.
    for mod_name in ("kasa.discover", "kasa.device"):
        mod = __import__(mod_name, fromlist=["get_protocol"])
        if getattr(mod, "get_protocol", None) is original:
            mod.get_protocol = get_protocol


def resolved_imports() -> dict:
    """What we managed to import, for diagnostics."""
    return {
        "IotProtocol": getattr(IotProtocol, "__module__", None),
        "KlapTransport": getattr(KlapTransport, "__module__", None),
        "KlapTransportV2": getattr(KlapTransportV2, "__module__", None),
        "XorTransport": getattr(XorTransport, "__module__", None),
    }


def apply(force_xor_hosts=()) -> None:
    """Apply both fallbacks. Safe to call more than once.

    Intended to be run from an executor (see __init__.py) so the python-kasa
    imports in _load_kasa() do not block Home Assistant's event loop.
    """
    if IotProtocol is None:
        _load_kasa()
    missing = [n for n, v in {
        "IotProtocol": IotProtocol, "KlapTransport": KlapTransport,
        "KlapTransportV2": KlapTransportV2, "XorTransport": XorTransport,
    }.items() if v is None]
    if missing:
        raise RuntimeError(
            "kasa_klap_fix could not import from python-kasa: "
            + ", ".join(missing)
            + f". Resolved: {resolved_imports()}"
        )
    FORCE_XOR_HOSTS.update(force_xor_hosts)
    if getattr(KlapTransport, _PATCH_FLAG, False):
        return
    _patch_handshake()
    _patch_iot_query()
    _patch_factory()
    setattr(KlapTransport, _PATCH_FLAG, True)
    _LOGGER.info(
        "kasa_klap_fix applied (XOR forced for: %s)",
        ", ".join(sorted(FORCE_XOR_HOSTS)) or "none",
    )
