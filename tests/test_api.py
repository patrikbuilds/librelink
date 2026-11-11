"""No real accounts, HTTP requests or Home Assistant install required."""
import ast
import asyncio
import hashlib
import importlib
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('_librelink_test')
package.__path__ = [str(ROOT / 'custom_components/librelink')]
sys.modules[package.__name__] = package
api = importlib.import_module('_librelink_test.api')
const = importlib.import_module('_librelink_test.const')


def response(body=None, status=200):
    value = Mock(status=status)
    value.json = AsyncMock(return_value=body)
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=value)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def success():
    return {'status': 0, 'data': {'authTicket': {'token': 'SECRET-TOKEN'}, 'user': {'id': 'private-id'}}}


def load_function(filename, name, globals):
    tree = ast.parse((ROOT / 'custom_components/librelink' / filename).read_text())
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name]
    tree = ast.Module(body=[nodes[0]], type_ignores=[])
    env = dict(globals)
    exec(compile(tree, filename, 'exec'), env)
    return env[name]


class ApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Mock()
        self.session.request.return_value = response(success())
        self.login = api.LibreLinkApiLogin('test@example.invalid', 'SECRET-PASSWORD', const.BASE_URL_LIST['Europe'], self.session)

    async def test_login_uses_new_version_and_hashes_account_id(self):
        self.assertEqual(await self.login.async_get_token(), 'SECRET-TOKEN')
        request = self.session.request.call_args.kwargs
        self.assertEqual(request['headers']['version'], '4.16.0')
        self.assertFalse(request['allow_redirects'])
        self.assertEqual(self.login.auth.account_id, hashlib.sha256(b'private-id').hexdigest())
        self.assertNotIn('SECRET-TOKEN', repr(self.login.auth))
        self.assertEqual(self.login._password, '')

    async def test_region_redirect_and_connections_use_final_region(self):
        self.session.request.side_effect = [
            response({'status': 0, 'data': {'redirect': True, 'region': 'eu2'}}),
            response(success()), response({'status': 0, 'data': [{'patientId': 'b'}, {'patientId': 'a'}]})]
        await self.login.async_get_token()
        auth = self.login.auth
        client = api.LibreLinkApiClient(auth.token, auth.base_url, self.session, auth.account_id)
        self.assertEqual(await client.async_get_data(), [{'patientId': 'a'}, {'patientId': 'b'}])
        request = self.session.request.call_args.kwargs
        self.assertEqual(request['url'], 'https://api-eu2.libreview.io/llu/connections')
        self.assertEqual(request['headers']['Account-Id'], auth.account_id)

    async def test_unknown_region_does_not_receive_credentials(self):
        for region in ('evil.example', 'https://evil.example', None):
            self.session.request.return_value = response({'status': 0, 'data': {'redirect': True, 'region': region}})
            login = api.LibreLinkApiLogin('user', 'password', const.BASE_URL_LIST['Europe'], self.session)
            self.session.request.reset_mock()
            with self.assertRaises(api.LibreLinkApiError):
                await login.async_get_token()
            self.assertEqual(self.session.request.call_count, 1)

    async def test_redirect_cycles_and_chain_are_bounded(self):
        for regions in (['eu'], ['eu2', 'de', 'fr']):
            self.session.request.side_effect = [response({'status': 0, 'data': {'redirect': True, 'region': region}}) for region in regions]
            login = api.LibreLinkApiLogin('user', 'password', const.BASE_URL_LIST['Europe'], self.session)
            self.session.request.reset_mock()
            with self.assertRaises(api.LibreLinkApiError):
                await login.async_get_token()
            self.assertLessEqual(self.session.request.call_count, 3)

    async def test_http_rejections_are_specific_and_do_not_retry(self):
        for status in (401, 403, 429, 430, 302, 500):
            self.session.request.reset_mock()
            self.session.request.return_value = response(status=status)
            error_type = api.LibreLinkApiAuthenticationError if status in (401, 403) else api.LibreLinkApiCommunicationError
            with self.subTest(status=status), self.assertRaises(error_type) as caught:
                await self.login.async_get_token()
            self.assertIn('HTTP', str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertEqual(self.session.request.call_count, 1)

    async def test_application_errors_and_invalid_login_responses(self):
        for body in ([], {}, {'status': 2}, {'status': 4}, {'status': 920}, {'status': 429},
                     {'status': 0, 'data': []}, {'status': 0, 'data': {}},
                     {'status': 0, 'data': {'authTicket': {'token': 'SECRET-TOKEN'}, 'user': {}}}):
            self.session.request.return_value = response(body)
            with self.subTest(body=body), self.assertRaises(api.LibreLinkApiError) as caught:
                await self.login.async_get_token()
            self.assertNotIn('SECRET', str(caught.exception))

    async def test_network_and_json_errors_do_not_disclose_request_data(self):
        for error in (TimeoutError('SECRET'), OSError('SECRET'), api.aiohttp.ClientError('SECRET')):
            self.session.request.side_effect = error
            with self.assertRaises(api.LibreLinkApiCommunicationError) as caught:
                await self.login.async_get_token()
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
        self.session.request.side_effect = None
        context = response()
        context.__aenter__.return_value.json.side_effect = ValueError('SECRET')
        self.session.request.return_value = context
        with self.assertRaises(api.LibreLinkApiError):
            await self.login.async_get_token()

    async def test_debug_logs_contain_no_credentials_tokens_or_patient_details(self):
        with self.assertLogs(api._LOGGER, logging.DEBUG) as logs:
            await self.login.async_get_token()
        output = '\n'.join(logs.output)
        for value in ('SECRET-PASSWORD', 'SECRET-TOKEN', 'private-id', 'test@example.invalid'):
            self.assertNotIn(value, output)

    async def test_setup_consumes_validation_session_without_second_login(self):
        hass = types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace(async_forward_entry_setups=AsyncMock()))
        globals = {
            'LibreLinkApiLogin': api.LibreLinkApiLogin, 'async_get_clientsession': lambda h: self.session,
            'DOMAIN': const.DOMAIN, 'auth_key': api.auth_key, 'remember_auth': api.remember_auth,
        }
        validate = load_function('config_flow.py', '_test_credentials', globals)
        await validate(types.SimpleNamespace(hass=hass), 'user', 'password', const.BASE_URL_LIST['Europe'])
        entry = Mock(data={'username': 'user', 'password': 'password', 'Country': 'Europe'}, entry_id='test-entry')
        coordinator = Mock(async_config_entry_first_refresh=AsyncMock())
        globals.update({
            'HomeAssistant': object, 'ConfigEntry': object, '_LOGGER': Mock(),
            'BASE_URL_LIST': const.BASE_URL_LIST, 'COUNTRY': const.COUNTRY,
            'CONF_USERNAME': 'username', 'CONF_PASSWORD': 'password',
            'take_auth': api.take_auth, 'LibreLinkApiClient': api.LibreLinkApiClient,
            'LibreLinkDataUpdateCoordinator': Mock(return_value=coordinator), 'PLATFORMS': [],
            'async_reload_entry': Mock(),
            'LibreLinkApiAuthenticationError': api.LibreLinkApiAuthenticationError,
            'LibreLinkApiError': api.LibreLinkApiError, 'ConfigEntryAuthFailed': RuntimeError,
            'ConfigEntryNotReady': RuntimeError,
        })
        setup = load_function('__init__.py', 'async_setup_entry', globals)
        self.assertTrue(await setup(hass, entry))
        self.assertEqual(self.session.request.call_count, 1)
        coordinator.async_config_entry_first_refresh.assert_awaited_once()
        self.assertEqual(globals['LibreLinkDataUpdateCoordinator'].call_args.kwargs['client'].account_id,
                         hashlib.sha256(b'private-id').hexdigest())


class CacheTest(unittest.TestCase):
    def test_cache_is_one_use_credential_scoped_and_expires(self):
        key = api.auth_key('user', 'password', 'https://api-eu.libreview.io')
        self.assertNotEqual(key, api.auth_key('user', 'changed', 'https://api-eu.libreview.io'))
        self.assertNotEqual(key, api.auth_key('user', 'password', 'https://api.libreview.io'))
        auth = api.AuthSession('token', 'account', 'url')
        cache = {}
        api.remember_auth(cache, key, auth)
        self.assertIs(api.take_auth(cache, key), auth)
        self.assertIsNone(api.take_auth(cache, key))
        cache[key] = auth
        with patch.object(api.time, 'monotonic', return_value=auth.created + 61):
            self.assertIsNone(api.take_auth(cache, key))
            cache['stale'] = auth
            api.remember_auth(cache, 'new', auth)
            self.assertNotIn('stale', cache)
