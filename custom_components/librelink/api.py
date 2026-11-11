"""LibreLinkUp requests with bounded regional routing and redacted errors."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import time

import aiohttp

from .const import API_TIME_OUT_SECONDS, BASE_URL_LIST, LOGIN_URL, CONNECTION_URL, PRODUCT, VERSION_APP

_LOGGER = logging.getLogger(__name__)
REGION_URLS = {
    'ae': BASE_URL_LIST['Arab Emirates'], 'ap': BASE_URL_LIST['Asia Pacific'],
    'au': BASE_URL_LIST['Australia'], 'ca': BASE_URL_LIST['Canada'],
    'de': BASE_URL_LIST['Germany'], 'eu': BASE_URL_LIST['Europe'],
    'eu2': BASE_URL_LIST['Europe 2'], 'gb': BASE_URL_LIST['Europe 2'],
    'uk': BASE_URL_LIST['Europe 2'], 'fr': BASE_URL_LIST['France'],
    'jp': BASE_URL_LIST['Japan'], 'ru': BASE_URL_LIST['Russia'],
    'us': BASE_URL_LIST['United States'], 'la': 'https://api-la.libreview.io',
}


class LibreLinkApiError(Exception):
    """Safe API error with no request/response data."""


class LibreLinkApiCommunicationError(LibreLinkApiError):
    """A connection failure or server rejection."""


class LibreLinkApiAuthenticationError(LibreLinkApiError):
    """Credentials rejected or account action required."""


@dataclass
class AuthSession:
    token: str = field(repr=False)
    account_id: str = field(repr=False)
    base_url: str
    created: float = field(default_factory=time.monotonic)


def auth_key(username, password, base_url):
    """In-memory cache key, scoped to the exact credentials and starting region."""
    return hashlib.sha256(repr((username, password, base_url)).encode()).hexdigest()


def remember_auth(cache, key, auth):
    now = time.monotonic()
    for old in list(cache):
        if now - cache[old].created >= 60:
            del cache[old]
    cache[key] = auth


def take_auth(cache, key):
    """Consume validation auth once; never reuse old or persisted credentials."""
    auth = cache.pop(key, None)
    return auth if auth is not None and time.monotonic() - auth.created < 60 else None


async def api_wrapper(session, method, url, data=None, headers=None):
    """Do not follow HTTP redirects or chain exceptions containing headers/tokens."""
    try:
        async with asyncio.timeout(API_TIME_OUT_SECONDS):
            async with session.request(method=method, url=url, headers=headers,
                                       json=data, allow_redirects=False) as response:
                status = response.status
                _LOGGER.debug('LibreLinkUp HTTP status: %s', status)
                if status in (401, 403):
                    raise LibreLinkApiAuthenticationError('Login/access rejected (HTTP 401/403).')
                if status in (429, 430):
                    raise LibreLinkApiCommunicationError(
                        f'LibreLinkUp rejected the request (HTTP {status}); pause retries and check the LibreLinkUp app.')
                if status >= 300:
                    raise LibreLinkApiCommunicationError(f'LibreLinkUp request failed (HTTP {status}).')
                result = await response.json()
    except LibreLinkApiError:
        raise
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        raise LibreLinkApiCommunicationError('Unable to communicate with LibreLinkUp.') from None
    except (ValueError, TypeError):
        raise LibreLinkApiError('LibreLinkUp returned invalid JSON.') from None
    if not isinstance(result, dict):
        raise LibreLinkApiError('LibreLinkUp returned an invalid response.')
    return result


def check_status(result):
    status = result.get('status')
    if status == 0:
        return
    if status == 2:
        raise LibreLinkApiAuthenticationError('LibreLinkUp rejected the credentials.')
    if status == 4:
        raise LibreLinkApiAuthenticationError('Open LibreLinkUp and complete any pending account agreements.')
    if status == 920:
        raise LibreLinkApiError('LibreLinkUp requires a newer client version; update the integration.')
    if status in (429, 430):
        raise LibreLinkApiCommunicationError('LibreLinkUp temporarily rejected requests; pause retries.')
    # Never interpolate unknown response values into diagnostics.
    raise LibreLinkApiError('LibreLinkUp returned an unsuccessful or missing status.')


class LibreLinkApiLogin:
    def __init__(self, username, password, base_url, session):
        self._username = username
        self._password = password
        self.base_url = base_url
        self._session = session
        self.auth = None

    async def async_get_token(self):
        visited = set()
        for _ in range(3):
            if self.base_url not in set(BASE_URL_LIST.values()) | set(REGION_URLS.values()) or self.base_url in visited:
                raise LibreLinkApiError('Unsupported or repeated LibreLinkUp region redirect.')
            visited.add(self.base_url)
            response = await api_wrapper(
                self._session, 'post', self.base_url + LOGIN_URL,
                headers={'product': PRODUCT, 'version': VERSION_APP, 'Accept': 'application/json'},
                data={'email': self._username, 'password': self._password})
            check_status(response)
            data = response.get('data')
            if not isinstance(data, dict):
                raise LibreLinkApiError('LibreLinkUp login response is missing account data.')
            if data.get('redirect'):
                region = data.get('region')
                self.base_url = REGION_URLS.get(region.lower()) if isinstance(region, str) else None
                continue
            ticket, user = data.get('authTicket'), data.get('user')
            token = ticket.get('token') if isinstance(ticket, dict) else None
            user_id = user.get('id') if isinstance(user, dict) else None
            if not isinstance(token, str) or not token or not isinstance(user_id, str) or not user_id:
                raise LibreLinkApiError('LibreLinkUp login response is missing a token or user ID.')
            self.auth = AuthSession(token, hashlib.sha256(user_id.encode()).hexdigest(), self.base_url)
            self._password = ''
            return token
        raise LibreLinkApiError('Too many LibreLinkUp region redirects.')


class LibreLinkApiClient:
    def __init__(self, token, base_url, session, account_id=None):
        self._token = token
        self._session = session
        self.account_id = account_id
        self.connection_url = base_url + CONNECTION_URL

    async def async_get_data(self):
        headers = {'product': PRODUCT, 'version': VERSION_APP, 'Accept': 'application/json',
                   'Authorization': 'Bearer ' + self._token}
        if self.account_id:
            headers['Account-Id'] = self.account_id
        response = await api_wrapper(self._session, 'get', self.connection_url, headers=headers)
        check_status(response)
        patients = response.get('data')
        if not isinstance(patients, list) or any(not isinstance(p, dict) or not isinstance(p.get('patientId'), str) for p in patients):
            raise LibreLinkApiError('LibreLinkUp returned invalid connection data.')
        return sorted(patients, key=lambda p: p['patientId'])


class LibreLinkGetGraph(LibreLinkApiClient):
    """Legacy unused helper retained for import compatibility."""
    def __init__(self, token, base_url, session, patient_id, account_id=None):
        super().__init__(token, base_url, session, account_id)
        self.patient_id = patient_id
