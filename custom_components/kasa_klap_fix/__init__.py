"""Patch python-kasa so KLAP-failing Kasa devices work again.

Add to configuration.yaml:

    kasa_klap_fix:
"""

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "kasa_klap_fix"
CONF_FORCE_XOR_HOSTS = "force_xor_hosts"

_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_FORCE_XOR_HOSTS, default=list): vol.All(
            cv.ensure_list, [cv.string]
        )
    }
)

# `kasa_klap_fix:` with nothing after it parses as None, not {}.
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Any(_OPTIONS_SCHEMA, None)},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Apply the patch before the tplink integration connects."""
    domain_config = config.get(DOMAIN) or {}
    hosts = domain_config.get(CONF_FORCE_XOR_HOSTS) or []
    try:
        # Imported here, not at module level, so an import problem is reported
        # in the log instead of silently preventing the integration from
        # loading. This import is cheap: patch.py defers the python-kasa
        # imports into apply(), which runs in the executor below.
        from .patch import apply, resolved_imports

        await hass.async_add_executor_job(apply, hosts)
        _LOGGER.debug("kasa_klap_fix: resolved %s", resolved_imports())
    except Exception:
        _LOGGER.exception("kasa_klap_fix: FAILED to apply patch")
        return False
    _LOGGER.info(
        "kasa_klap_fix: patch applied (XOR forced for: %s)",
        ", ".join(hosts) if hosts else "no hosts",
    )
    return True
