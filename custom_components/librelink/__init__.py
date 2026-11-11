"""Custom integration to integrate LibreLink with Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (LibreLinkApiClient, LibreLinkApiLogin, auth_key, take_auth,
                  LibreLinkApiAuthenticationError, LibreLinkApiError)
from .const import BASE_URL_LIST, COUNTRY, DOMAIN
from .coordinator import LibreLinkDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""

    _LOGGER.debug("Setting up LibreLink entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})

    base_url = BASE_URL_LIST.get(entry.data[COUNTRY])
    session = async_get_clientsession(hass)
    cache = hass.data[DOMAIN].setdefault('_auth_cache', {})
    auth = take_auth(cache, auth_key(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], base_url))
    if auth is None:
        login = LibreLinkApiLogin(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], base_url, session)
        try:
            await login.async_get_token()
        except LibreLinkApiAuthenticationError as error:
            raise ConfigEntryAuthFailed(str(error)) from None
        except LibreLinkApiError as error:
            raise ConfigEntryNotReady(str(error)) from None
        auth = login.auth
    myLibrelinkClient = LibreLinkApiClient(
        auth.token, auth.base_url, session, account_id=auth.account_id)

    hass.data[DOMAIN][entry.entry_id] = coordinator = LibreLinkDataUpdateCoordinator(
        hass=hass,
        client=myLibrelinkClient,
    )

    # First poll of the data to be ready for entities initialization
    await coordinator.async_config_entry_first_refresh()

    # Then launch async_setup_entry for our declared entities in sensor.py and binary_sensor.py
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload entry when its updated.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry  when it changed."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
