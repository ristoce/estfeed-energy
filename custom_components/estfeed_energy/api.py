"""Elering customer-portal API. No Home Assistant dependencies."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
import aiohttp

TOKEN_URL = 'https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token'
API_URL = 'https://estfeed.elering.ee/api/public/v1/'

class ApiError(Exception):
    """Sanitized API error (never contains response bodies or credentials)."""

class AuthError(ApiError):
    """Credentials expired, revoked, or unauthorized."""

class RetryableError(ApiError):
    def __init__(self, status, delay=0):
        super().__init__(f'Temporary HTTP {status}')
        self.delay = delay


def retry_after(value):
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return 0.0

class Client:
    def __init__(self, session, client_id, secret, spacing=6.0):
        self.session = session
        self.client_id = client_id
        self.secret = secret
        self.spacing = max(6.0, spacing)
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.token = None
        self.expires = 0.0

    async def _request(self, method, url, **kwargs):
        # Also throttle token calls and every failed attempt. Hold lock in get().
        await asyncio.sleep(max(0.0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + self.spacing
        async with self.session.request(
            method, url, timeout=aiohttp.ClientTimeout(total=60),
            allow_redirects=False, **kwargs
        ) as response:
            if response.status == 429 or response.status >= 500 or response.status == 408:
                delay = retry_after(response.headers.get('Retry-After'))
                self.next_request = max(self.next_request, time.monotonic() + delay)
                raise RetryableError(response.status, delay)
            if response.status in (400, 401, 403) and url == TOKEN_URL:
                raise AuthError(f'Token endpoint HTTP {response.status}; check API key')
            if response.status == 401:
                self.token = None
                raise RetryableError(401)
            if response.status == 403:
                raise AuthError('Metering API HTTP 403; check API key access')
            if response.status != 200:
                raise ApiError(f'API HTTP {response.status}')
            try:
                return await response.json(content_type=None)
            except (ValueError, aiohttp.ClientError) as err:
                raise RetryableError('invalid JSON') from err

    async def get(self, endpoint, params):
        async with self.lock:
            for attempt in range(4):
                try:
                    if self.token is None or time.monotonic() >= self.expires:
                        result = await self._request('POST', TOKEN_URL, data={
                            'grant_type': 'client_credentials',
                            'client_id': self.client_id, 'client_secret': self.secret,
                        })
                        if not isinstance(result, dict) or not result.get('access_token'):
                            raise ApiError('Invalid token response')
                        self.token = result['access_token']
                        self.expires = time.monotonic() + max(0, float(result.get('expires_in', 300)) - 30)
                    result = await self._request('GET', API_URL + endpoint, params=params,
                        headers={'Authorization': 'Bearer ' + self.token})
                    if not isinstance(result, list):
                        raise ApiError('Expected a list from metering API')
                    return result
                except (RetryableError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                    if attempt == 3:
                        raise ApiError('API unavailable after four attempts; will retry later') from err
                    self.next_request = max(self.next_request, time.monotonic() + 10 * 2**attempt + random.uniform(0, 2))
            raise ApiError('Request failed')

    async def _public_get(self, url, params):
        """Unauthenticated pricing requests share metering's lock and throttle."""
        async with self.lock:
            for attempt in range(4):
                try:
                    return await self._request('GET', url, params=params)
                except (RetryableError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                    if attempt == 3:
                        raise ApiError('Price API unavailable after four attempts') from err
                    self.next_request = max(self.next_request, time.monotonic() + 10 * 2**attempt + random.uniform(0, 2))

    async def prices(self, start, end):
        """Public Estonian electricity prices."""
        return await self._public_get('https://dashboard.elering.ee/api/nps/price',
                                      {'start': start, 'end': end})

    async def estfeed_electricity_prices(self, start, end):
        """Fallback public electricity prices with explicit quarter-hour bounds."""
        return await self._public_get(API_URL + 'energy-price/electricity', {
            'startDateTime': start, 'endDateTime': end, 'resolution': 'fifteen_minutes'})

    async def gas_prices(self, start, end):
        """Public Estfeed daily gas prices, without an Authorization header."""
        return await self._public_get(API_URL + 'energy-price/gas', {
            'startDateTime': start, 'endDateTime': end, 'resolution': 'one_day'})
