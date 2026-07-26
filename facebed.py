import argparse
import base64
import io
import json
import logging
import math
import os
import queue
import re
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from functools import wraps
from html import escape, unescape
from pathlib import Path
from typing import Self, Callable
from urllib.parse import quote as _quote_
from urllib.parse import parse_qs, urlparse

import crawleruseragents
from curl_cffi.requests import Session as CffiSession
from curl_cffi.requests import Response as CffiResponse
from curl_cffi.requests import RequestsError
import yaml
from bottle import Bottle, request, response, static_file
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook, DiscordEmbed
from yattag import indent


DEFAULT_FACEBOOK_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36'
SHARE_HEAD_USER_AGENT = 'python-requests/2.32.3'
SHARE_BODY_USER_AGENT = 'Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)'


class FacebedException(Exception):
    pass


class NoDataException(FacebedException):
    pass


class UnsupportedRouteException(NoDataException):
    pass


class ShareResolutionException(NoDataException):
    def __init__(
        self,
        message: str,
        response: CffiResponse | None = None,
        count_as_error: bool = False,
    ):
        super().__init__(message)
        self.upstream_response = response
        self.count_as_error = count_as_error
        self.account_backed = False


class ParseException(FacebedException):
    def __init__(self, message: str, html: str = '', url: str = ''):
        super().__init__(message)
        self.html = html
        self.url = url


class UpstreamException(FacebedException):
    def __init__(
        self,
        message: str,
        response: CffiResponse | None = None,
        transport_error: bool | None = None,
    ):
        super().__init__(message)
        self.upstream_response = response
        self.transport_error = (
            response is None if transport_error is None else transport_error
        )
        self.status_code = getattr(response, 'status_code', None)
        self.retry_after = None
        if response is not None:
            self.retry_after = response.headers.get('Retry-After')


class CFFI:
    impersonate: str = 'chrome146'
    timeout: tuple[float, float] = (5.0, 20.0)
    retry_statuses: set[int] = {429, 500, 502, 503, 504}
    max_attempts: int = 2

    def __init__(self) -> None:
        self._local = threading.local()

    @contextmanager
    def request_scope(self, account=None):
        if getattr(self._local, 'session', None) is not None:
            yield self._local.session
            return

        session_headers = JsonParser.get_headers()
        session_headers = {
            key: value
            for key, value in session_headers.items()
            if not key.lower().startswith('sec-ch-')
        }
        session_headers['user-agent'] = DEFAULT_FACEBOOK_USER_AGENT
        session = CffiSession(
            impersonate=self.impersonate,
            default_headers=False,
            headers=session_headers,
            discard_cookies=True,
        )
        self._local.session = session
        self._local.account = account
        self._local.get_cache = {}
        self._local.responses = []
        self._local.last_get_response = None
        self._local.selected_get_response = None
        try:
            yield session
        finally:
            try:
                session.close()
            finally:
                for attr in (
                    'session', 'account', 'get_cache', 'responses', 'last_get_response',
                    'selected_get_response', 'affinity_path',
                ):
                    if hasattr(self._local, attr):
                        delattr(self._local, attr)

    def _get_session(self) -> CffiSession:
        return getattr(self._local, 'session', None)

    @property
    def last_get_response(self) -> CffiResponse | None:
        return getattr(self._local, 'last_get_response', None)

    @property
    def selected_get_response(self) -> CffiResponse | None:
        return getattr(self._local, 'selected_get_response', None)

    @property
    def current_account(self):
        return getattr(self._local, 'account', None)

    @property
    def affinity_path(self) -> str | None:
        return getattr(self._local, 'affinity_path', None)

    def set_affinity_path(self, path: str) -> None:
        self._local.affinity_path = path

    def select_response(self, response: CffiResponse | None) -> None:
        if response is not None:
            self._local.selected_get_response = response

    def get(self, url: str, **kwargs) -> CffiResponse:
        return self._request('GET', url, **kwargs)

    def head(self, url: str, **kwargs) -> CffiResponse:
        return self._request('HEAD', url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> CffiResponse:
        sess = self._get_session()
        if sess is None:
            with self.request_scope():
                return self._request(method, url, **kwargs)

        method = method.upper()
        check_status = kwargs.pop('_check_status', True)
        retry_status_responses = kwargs.pop('_retry_status_responses', True)
        bypass_cache = kwargs.pop('_bypass_cache', False)
        use_cookies = kwargs.pop('_use_cookies', True)
        kwargs.pop('cookies', None)
        headers = dict(kwargs.pop('headers', {}))
        headers = self._request_headers(url, headers, use_cookies)
        kwargs.setdefault('allow_redirects', True)
        kwargs.setdefault('timeout', self.timeout)
        kwargs.setdefault('max_redirects', 10)
        cache = getattr(self._local, 'get_cache', {})
        def freeze(value):
            if isinstance(value, dict):
                return tuple(sorted((str(key), freeze(item)) for key, item in value.items()))
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, set):
                return tuple(sorted(freeze(item) for item in value))
            try:
                hash(value)
                return value
            except TypeError:
                return repr(value)

        cache_key = (
            url,
            freeze({
                'headers': headers,
                **{key: value for key, value in kwargs.items() if key != 'timeout'},
            }),
        )
        if method == 'GET' and not bypass_cache and cache_key in cache:
            cached = cache[cache_key]
            self._local.last_get_response = cached
            if check_status and cached.status_code >= 400:
                raise UpstreamException(
                    f'Facebook returned HTTP {cached.status_code} for {url}', cached
                )
            return cached

        last_error: RequestsError | None = None
        last_response: CffiResponse | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                request_kwargs = dict(kwargs)
                if headers:
                    request_kwargs['headers'] = headers
                upstream_response = sess.request(method, url, **request_kwargs)
                last_response = upstream_response
                content_bytes = len(upstream_response.content or b'') if method == 'GET' else 0
                final_url = str(upstream_response.url)
                status_code = int(upstream_response.status_code)
                history = getattr(upstream_response, 'history', []) or []
                logging.info(
                    'upstream %s %s -> %s status=%s redirects=%s bytes=%s attempt=%s',
                    method, url, final_url, status_code, len(history), content_bytes, attempt,
                )
                self._local.responses.append({
                    'method': method,
                    'requested_url': url,
                    'final_url': final_url,
                    'status': status_code,
                    'bytes': content_bytes,
                    'attempt': attempt,
                })
                if method == 'GET':
                    cache[cache_key] = upstream_response
                    self._local.last_get_response = upstream_response

                if (
                    retry_status_responses
                    and status_code in self.retry_statuses
                    and attempt < self.max_attempts
                ):
                    time.sleep(0.25)
                    continue
                if check_status and status_code >= 400:
                    raise UpstreamException(
                        f'Facebook returned HTTP {status_code} for {url}', upstream_response
                    )
                return upstream_response
            except RequestsError as exc:
                last_error = exc
                logging.warning(
                    'upstream %s %s failed on attempt %s/%s: %s',
                    method, url, attempt, self.max_attempts, exc,
                )
                if attempt < self.max_attempts and method in ('GET', 'HEAD'):
                    time.sleep(0.25)
                    continue
                break

        raise UpstreamException(
            f'Facebook request failed for {url}: {last_error}',
            last_response,
            transport_error=True,
        ) from last_error

    def _request_headers(
        self,
        url: str,
        headers: dict,
        use_cookies: bool,
    ) -> dict:
        account = getattr(self._local, 'account', None)
        hostname = (urlparse(url).hostname or '').lower()
        is_facebook = hostname == 'facebook.com' or hostname.endswith('.facebook.com')
        result = {
            key: value
            for key, value in headers.items()
            if str(key).lower() != 'cookie'
        }
        if account is None or not use_cookies or not is_facebook:
            return result
        result = {
            key: value
            for key, value in result.items()
            if str(key).lower() != 'user-agent'
        }
        result['Cookie'] = account.header_value()
        result['User-Agent'] = (
            account.user_agent
            if account.user_agent is not None
            else DEFAULT_FACEBOOK_USER_AGENT
        )
        return result


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / 'assets'

CONFIG_STR = '''
host: 0.0.0.0
port: 9812
timezone: 7
banned_users: []
notifier_webhook: ''
'''.strip()

config: dict = {}
default_config: dict = yaml.safe_load(io.StringIO(CONFIG_STR))
app: Bottle = Bottle()
cffi = CFFI()


class ServiceMetrics:
    def __init__(self, started_at: float | None = None) -> None:
        self._started_at = time.monotonic() if started_at is None else started_at
        self._requests = 0
        self._errors = 0
        self._lock = threading.Lock()

    def record_request(self) -> None:
        with self._lock:
            self._requests += 1

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            uptime_secs = max(0, int(time.monotonic() - self._started_at))
            return uptime_secs, self._requests, self._errors


service_metrics = ServiceMetrics()

WWWFB = 'https://www.facebook.com'
FACEBOOK_REACTION_EMOJIS = {
    '1635855486666999': '👍',
    '1678524932434102': '❤️',
    '613557422527858': '🤗',
    '115940658764963': '😂',
    '478547315650144': '😮',
    '908563459236466': '😢',
    '444813342392137': '😡',
}
FACEBOOK_REACTION_NAME_IDS = {
    'like': '1635855486666999',
    'love': '1678524932434102',
    'care': '613557422527858',
    'haha': '115940658764963',
    'wow': '478547315650144',
    'sad': '908563459236466',
    'angry': '444813342392137',
}
logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(message)s', level=logging.INFO)


def quote(s: str) -> str:
    return "".join([
        _quote_(char) if char in r"<>\"'#%{}[]|\\^~`" else char
        for char in s
    ])

def get_credit() -> str:
    return 'facebed by pi.kt'


class Utils:
    @staticmethod
    def normalize_facebook_path(url_or_path: str) -> str:
        value = str(url_or_path)
        parsed = urlparse(value)
        if not parsed.netloc and re.match(
            r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE
        ):
            parsed = urlparse(f'https://{value}')
        if not parsed.netloc:
            return value.lstrip('/')

        hostname = (parsed.hostname or '').lower()
        if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
            normalized = parsed.path.lstrip('/')
            if parsed.query:
                normalized += f'?{parsed.query}'
            return normalized
        return value

    @staticmethod
    def is_share_path(path: str) -> bool:
        return bool(re.match(r'^/?share(?:/|$)', urlparse(path).path, re.IGNORECASE))

    @staticmethod
    def is_group_landing_target(path: str) -> bool:
        normalized_path = urlparse(JsonParser.ensure_full_url(path)).path.strip('/')
        return normalized_path.startswith('groups/') and (
            normalized_path.endswith('/about')
            or len(normalized_path.split('/')) <= 2
        )

    @staticmethod
    def resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        with cffi.request_scope():
            try:
                resolved_path, prefetched_response = Utils._resolve_share_link(path)
                resolved_url = JsonParser.ensure_full_url(resolved_path)
                if not urlparse(resolved_url).path.strip('/'):
                    raise ShareResolutionException(
                        'Facebook share resolution did not produce a post target'
                    )
                return resolved_path, prefetched_response
            except ShareResolutionException:
                raise
            except (NoDataException, UpstreamException) as exc:
                raise ShareResolutionException(
                    str(exc),
                    getattr(exc, 'upstream_response', None),
                    count_as_error=(
                        isinstance(exc, UpstreamException)
                        and exc.transport_error
                    ),
                ) from exc

    @staticmethod
    def _resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        source_path = Utils.normalize_facebook_path(path)
        url = JsonParser.ensure_full_url(source_path)
        is_share_v = urlparse(source_path).path.strip('/').startswith('share/v/')
        logging.info(f'resolving share link {url}')
        head_response = None
        try:
            head_response = cffi.head(
                url,
                headers={'User-Agent': SHARE_HEAD_USER_AGENT},
                _check_status=False,
            )
        except UpstreamException as exc:
            logging.warning('share HEAD failed for %s: %s', url, exc)

        needs_get = head_response is None
        if head_response is not None:
            head_url = str(head_response.url)
            head_path = Utils.normalize_facebook_path(head_url)
            parsed_head_url = urlparse(head_url)
            head_url_path = parsed_head_url.path.lower()
            head_hostname = (parsed_head_url.hostname or '').lower()
            head_is_facebook = (
                head_hostname == 'facebook.com'
                or head_hostname.endswith('.facebook.com')
            )
            normalized_head_path = head_url_path.strip('/')
            head_is_group_landing = Utils.is_group_landing_target(head_path)
            head_is_post_like = (
                normalized_head_path.startswith('watch')
                or normalized_head_path.startswith('reel/')
                or '/videos/' in normalized_head_path
                or (
                    normalized_head_path.startswith('groups/')
                    and (
                        '/permalink/' in normalized_head_path
                        or '/posts/' in normalized_head_path
                    )
                )
            )
            head_usable = (
                head_is_facebook
                and not head_is_group_landing
                and (not is_share_v or head_is_post_like)
            )
            needs_get = (
                head_response.status_code >= 400
                or Utils.is_share_path(head_path)
                or head_url_path.startswith('/login')
                or not head_usable
            )
            if not needs_get:
                logging.info(f'resolved to {head_response.url}')
                return head_path, None

        def inspect_direct(response: CffiResponse):
            response_url = str(response.url)
            parsed_response_url = urlparse(response_url)
            response_host = (parsed_response_url.hostname or '').lower()
            response_is_facebook = (
                response_host == 'facebook.com'
                or response_host.endswith('.facebook.com')
            )
            direct_url = response_url
            html_parser = BeautifulSoup(response.text, 'html.parser')
            canonical_candidates = []
            canonical_link = html_parser.select_one('link[rel="canonical"]')
            if canonical_link and canonical_link.get('href'):
                canonical_candidates.append(str(canonical_link['href']))
            for canonical_meta in html_parser.select('meta[property="og:url"]'):
                if canonical_meta and canonical_meta.get('content'):
                    canonical_candidates.append(str(canonical_meta['content']))
            declared_target = False
            for canonical_url in canonical_candidates:
                candidate_url = JsonParser.ensure_full_url(canonical_url)
                candidate_path = Utils.normalize_facebook_path(candidate_url)
                parsed_candidate = urlparse(candidate_url)
                candidate_host = (parsed_candidate.hostname or '').lower()
                candidate_is_facebook = (
                    candidate_host == 'facebook.com'
                    or candidate_host.endswith('.facebook.com')
                )
                candidate_path_only = parsed_candidate.path.strip('/')
                candidate_first_segment = candidate_path_only.partition('/')[0].lower()
                candidate_page = candidate_first_segment.removesuffix('.php')
                candidate_is_usable = (
                    bool(candidate_path_only)
                    and not Utils.is_share_path(candidate_path)
                    and candidate_page not in {'login', 'checkpoint', 'recover'}
                    and not Utils.is_group_landing_target(candidate_path)
                )
                if candidate_is_facebook and candidate_is_usable:
                    direct_url = candidate_url
                    declared_target = True
                    break
            direct_path = Utils.normalize_facebook_path(direct_url)
            page_type = 'unknown'
            if response_is_facebook:
                page_type = JsonParser.probe_page_type(html_parser, direct_path)
                if page_type != 'has_data' and source_path != direct_path:
                    page_type = JsonParser.probe_page_type(html_parser, source_path)
            return (
                direct_url,
                direct_path,
                page_type,
                declared_target,
                response_is_facebook,
            )

        direct_response = cffi.get(
            url,
            headers={'User-Agent': SHARE_BODY_USER_AGENT},
            _check_status=False,
            _retry_status_responses=False,
        )
        (
            direct_url,
            direct_path,
            page_type,
            declared_target,
            response_is_facebook,
        ) = inspect_direct(direct_response)
        if (
            page_type != 'has_data'
            and direct_response.status_code in cffi.retry_statuses
        ):
            direct_response = cffi.get(
                url,
                headers={'User-Agent': SHARE_BODY_USER_AGENT},
                _check_status=False,
                _retry_status_responses=False,
                _bypass_cache=True,
            )
            (
                direct_url,
                direct_path,
                page_type,
                declared_target,
                response_is_facebook,
            ) = inspect_direct(direct_response)
        response_url_path = urlparse(str(direct_response.url)).path
        logging.info(f'resolved to {direct_url}')
        if not response_is_facebook:
            if declared_target:
                if Utils.is_group_landing_target(direct_path):
                    raise NoDataException(
                        'Facebook resolved share link to a group landing page'
                    )
                return direct_path, None
            raise NoDataException('Facebook redirected share link outside Facebook')
        if direct_response.status_code >= 400 and page_type != 'has_data':
            raise UpstreamException(
                f'Facebook returned HTTP {direct_response.status_code} for {url}', direct_response
            )
        if Utils.is_share_path(direct_path):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException('Facebook left the share URL unresolved without post data')
        if response_url_path.lower().startswith('/login'):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException('Facebook redirected share link to login')

        if Utils.is_group_landing_target(direct_path):
            raise NoDataException('Facebook resolved share link to a group landing page')
        return direct_path, direct_response if page_type == 'has_data' else None

    @staticmethod
    def prettify(txt: str) -> str:
        return indent(txt, indentation ='    ', newline = '\n', indent_text = True)

    @staticmethod
    def warn(msg: str = None, file_content: bytes = None, filename: str = None, embed: DiscordEmbed = None):
        def worker():
            wh = config.get('notifier_webhook', '')
            if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
                return
            try:
                webhook = DiscordWebhook(url=wh, content=msg)
                if embed:
                    webhook.add_embed(embed)
                if file_content and filename:
                    webhook.add_file(file=file_content, filename=filename)
                webhook.execute()
            except Exception:
                logging.warning(f"couldn't warn about {msg or embed}")

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def timestamp_to_str(ts: int) -> str:
        if ts < 0:
            return ''
        dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=config['timezone'])))
        tztext = dt.strftime('%z')[:3]
        return '⌚ ' + dt.strftime('%Y/%m/%d %H:%M:%S ') + f'UTC{tztext}'

    @staticmethod
    def human_format(num):
        if type(num) == int or re.match('^[0-9]+$', str(num)):
            num = int(num)
            num = float('{:.3g}'.format(num))
            magnitude = 0
            while abs(num) >= 1000:
                magnitude += 1
                num /= 1000.0
            return '{}{}'.format('{:f}'.format(num).rstrip('0').rstrip('.'), ['', 'K', 'M', 'B', 'T'][magnitude])
        else:
            return str(num)

    @staticmethod
    def get_top_reaction_ids(feedback: dict) -> tuple[str, ...]:
        if not isinstance(feedback, dict):
            return ()
        top_reactions = feedback.get('top_reactions')
        if not isinstance(top_reactions, dict):
            return ()
        edges = top_reactions.get('edges')
        if not isinstance(edges, list):
            return ()

        ranked: list[tuple[float, int, str]] = []
        seen_ids: set[str] = set()
        for position, edge in enumerate(edges):
            if not isinstance(edge, dict):
                continue
            node = edge.get('node')
            if not isinstance(node, dict):
                continue

            reaction_id = str(node.get('id') or '')
            if reaction_id not in FACEBOOK_REACTION_EMOJIS:
                reaction_name = str(
                    node.get('localized_name') or node.get('name') or ''
                ).casefold()
                reaction_id = FACEBOOK_REACTION_NAME_IDS.get(reaction_name, '')
            if not reaction_id or reaction_id in seen_ids:
                continue

            count = edge.get('reaction_count')
            if count is None:
                count = edge.get('i18n_reaction_count')
            text = str(count or '').strip().upper().replace(',', '')
            multiplier = 1
            if text.endswith(('K', 'M', 'B')):
                multiplier = {
                    'K': 1_000,
                    'M': 1_000_000,
                    'B': 1_000_000_000,
                }[text[-1]]
                text = text[:-1]
            try:
                numeric_count = float(text) * multiplier
            except (TypeError, ValueError):
                continue
            if numeric_count <= 0:
                continue

            seen_ids.add(reaction_id)
            ranked.append((-numeric_count, position, reaction_id))

        ranked.sort()
        reaction_ids = tuple(item[2] for item in ranked[:2])
        return reaction_ids if len(reaction_ids) == 2 else ()

    @staticmethod
    def format_reactions_str(
        likes: str,
        cmts: str,
        shares: str,
        top_reaction_ids: tuple[str, ...] = (),
    ) -> str:
        emojis: list[str] = []
        for reaction_id in top_reaction_ids:
            emoji = FACEBOOK_REACTION_EMOJIS.get(reaction_id)
            if emoji and emoji not in emojis:
                emojis.append(emoji)
        reaction_prefix = ' '.join(emojis[:2]) if len(emojis) >= 2 else '❤️'
        likes_str = f'{reaction_prefix} {likes}' if likes != 'null' else ''
        cmts_str = f'💬 {cmts}' if cmts != 'null' else ''
        shares_str = f'🔁 {shares}' if shares != 'null' else ''
        fmt = ' • '.join([x for x in [likes_str, cmts_str, shares_str] if x]).replace(',', '.')
        return fmt


class Jq:
    @staticmethod
    def enumerate(obj: dict):
        result = []

        def collect(value):
            if isinstance(value, dict):
                result.append(value)
                for v in value.values():
                    if isinstance(v, list):
                        collect(v)
                for v in value.values():
                    if isinstance(v, dict):
                        collect(v)
                for v in value.values():
                    if not isinstance(v, (dict, list)):
                        collect(v)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        collect(item)
                for item in value:
                    if isinstance(item, list):
                        collect(item)
                for item in value:
                    if not isinstance(item, (dict, list)):
                        collect(item)

        collect(obj)
        return result

    @staticmethod
    def iterate(obj: dict, key: str, first: bool = False):
        result = []
        for oo in Jq.enumerate(obj):
            if key in oo:
                if first:
                    return oo[key]
                else:
                    result.append(oo[key])
        return result

    @staticmethod
    def all(obj: dict, key: str) -> list[dict]:
        return Jq.iterate(obj, key, first=False)

    @staticmethod
    def first(obj: dict, key: str) -> dict:
        return Jq.iterate(obj, key, first=True)

    @staticmethod
    def has(obj: dict, *args: str) -> bool:
        for k in args:
            found = False
            for oo in Jq.enumerate(obj):
                if k in oo:
                    found = True
                    break
            if not found:
                return False
        return True

@dataclass(frozen=True)
class CookieEntry:
    name: str
    value: str
    expiration_date: float | None = None


class CookieAccount:
    def __init__(
        self,
        label: str,
        entries: list[CookieEntry],
        user_agent: str | None = None,
    ) -> None:
        self.label = label
        self.entries = entries
        self.user_agent = user_agent
        self._cookie_header = '; '.join(f'{entry.name}={entry.value}' for entry in entries)

    def header_value(self) -> str:
        return self._cookie_header

    def any_expired(self) -> bool:
        now = time.time()
        return any(
            entry.expiration_date is not None and entry.expiration_date <= now
            for entry in self.entries
        )


ACCOUNT_COOLDOWN_SECS = 300
RATE_LIMIT_COOLDOWN_SECS = 60
RATE_LIMIT_COOLDOWN_MAX_SECS = 600
CHECKPOINT_COOLDOWN_SECS = 1800
AFFINITY_CAP = 1024
NOTIFY_FAILURE_THRESHOLD = 3


class CookieJar:
    def __init__(self, accounts: list[CookieAccount] | None = None) -> None:
        self.accounts = list(accounts or [])
        self._cooldown_until = [0.0] * len(self.accounts)
        self._consecutive_failures = [0] * len(self.accounts)
        self._affinity: dict[str, int] = {}
        self._state_lock = threading.Lock()

    @classmethod
    def load(cls, path: Path | str) -> Self:
        cookie_path = Path(path)
        accounts: list[CookieAccount] = []
        seen: set[Path] = set()

        def load_one(candidate: Path) -> None:
            canonical = candidate.resolve()
            if canonical in seen:
                return
            seen.add(canonical)
            try:
                accounts.extend(cls._load_file(candidate))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                logging.warning('failed to load %s: %s', candidate, exc)

        if cookie_path.exists():
            load_one(cookie_path)
        else:
            logging.warning('%s not found', cookie_path)

        parent = cookie_path.parent
        try:
            siblings = sorted(
                candidate
                for candidate in parent.iterdir()
                if candidate.is_file()
                and candidate.name.startswith('cookies')
                and candidate.name.endswith('.json')
                and candidate.name != 'cookies.example.json'
            )
        except OSError as exc:
            logging.warning('could not scan %s for cookie files: %s', parent, exc)
            siblings = []
        for sibling in siblings:
            load_one(sibling)

        user_agents = cls._load_useragents(parent)
        for account in accounts:
            if account.user_agent is None and account.label in user_agents:
                account.user_agent = user_agents[account.label]

        cls._log_accounts(accounts)
        if not accounts:
            logging.warning('no cookies loaded, non incognito-viewable posts will NOT work')
        return cls(accounts)

    @classmethod
    def load_strict(cls, path: Path | str) -> Self:
        cookie_path = Path(path)
        parent = cookie_path.parent
        candidates = [cookie_path] if cookie_path.exists() else []
        candidates.extend(
            sorted(
                candidate
                for candidate in parent.iterdir()
                if candidate.is_file()
                and candidate.name.startswith('cookies')
                and candidate.name.endswith('.json')
                and candidate.name != 'cookies.example.json'
            )
        )
        accounts: list[CookieAccount] = []
        seen: set[Path] = set()
        for candidate in candidates:
            canonical = candidate.resolve()
            if canonical in seen:
                continue
            seen.add(canonical)
            accounts.extend(cls._load_file(candidate))
        user_agents = cls._load_useragents(parent)
        for account in accounts:
            if account.user_agent is None and account.label in user_agents:
                account.user_agent = user_agents[account.label]
        cls._log_accounts(accounts)
        return cls(accounts)

    @staticmethod
    def _log_accounts(accounts: list[CookieAccount]) -> None:
        for account in accounts:
            logging.info(
                "loaded %s cookies for account '%s'",
                len(account.entries),
                account.label,
            )
            if account.any_expired():
                logging.info(
                    "account '%s' has stale cookie expiration timestamps; live account check decides usability",
                    account.label,
                )

    @classmethod
    def _load_file(cls, cookie_path: Path) -> list[CookieAccount]:
        def reject_constant(value: str):
            raise ValueError(f'non-finite JSON number: {value}')

        def parse_float(value: str) -> float:
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError(f'non-finite JSON number: {value}')
            return parsed

        loaded = json.loads(
            cookie_path.read_text(encoding='utf-8'),
            parse_constant=reject_constant,
            parse_float=parse_float,
        )
        if isinstance(loaded, list):
            entries = cls._parse_entries(loaded)
            if not entries:
                return []
            stem = cookie_path.stem
            label = stem.removeprefix('cookies').lstrip('-_') or 'default'
            return [CookieAccount(label, entries)]
        if isinstance(loaded, dict):
            raw_accounts = loaded.get('accounts')
            if not isinstance(raw_accounts, list):
                raise ValueError('accounts must be a list')
            accounts = []
            for raw_account in raw_accounts:
                if not isinstance(raw_account, dict):
                    raise ValueError('account must be an object')
                label = raw_account.get('label')
                if not isinstance(label, str):
                    raise ValueError('account label must be a string')
                user_agent = raw_account.get('user_agent', raw_account.get('userAgent'))
                if user_agent is not None and not isinstance(user_agent, str):
                    raise ValueError('account user agent must be a string')
                accounts.append(
                    CookieAccount(
                        label,
                        cls._parse_entries(raw_account.get('entries')),
                        user_agent,
                    )
                )
            return accounts
        raise ValueError('unsupported cookies file shape')

    @staticmethod
    def _parse_entries(raw_entries: list) -> list[CookieEntry]:
        if not isinstance(raw_entries, list):
            raise ValueError('cookie entries must be a list')
        entries = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise ValueError('cookie entry must be an object')
            name = entry.get('name')
            value = entry.get('value')
            expiration = entry.get('expirationDate')
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError('cookie name and value must be strings')
            if expiration is not None and (
                isinstance(expiration, bool) or not isinstance(expiration, (int, float))
            ):
                raise ValueError('cookie expirationDate must be numeric')
            if expiration is not None:
                try:
                    expiration_is_finite = math.isfinite(expiration)
                except OverflowError:
                    expiration_is_finite = False
                if not expiration_is_finite:
                    raise ValueError('cookie expirationDate must be finite')
            entries.append(CookieEntry(name, value, expiration))
        return entries

    @staticmethod
    def _load_useragents(parent: Path) -> dict[str, str]:
        sidecar = parent / 'useragents.json'
        if not sidecar.exists():
            return {}
        try:
            loaded = json.loads(sidecar.read_text(encoding='utf-8'))
            if not isinstance(loaded, dict) or not all(
                isinstance(label, str) and isinstance(user_agent, str)
                for label, user_agent in loaded.items()
            ):
                raise ValueError('useragents.json must map labels to strings')
            return loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logging.warning('failed to parse %s: %s', sidecar, exc)
            return {}

    def len(self) -> int:
        return len(self.accounts)

    def is_empty(self) -> bool:
        return not self.accounts

    def account_at(self, index: int) -> CookieAccount | None:
        if not self.accounts:
            return None
        return self.accounts[index % len(self.accounts)]

    def _set_cooldown(self, index: int, seconds: int) -> None:
        if not self.accounts:
            return
        normalized = index % len(self.accounts)
        self._cooldown_until[normalized] = time.time() + seconds

    def mark_failed(self, index: int) -> int:
        if not self.accounts:
            return 0
        normalized = index % len(self.accounts)
        with self._state_lock:
            self._set_cooldown(normalized, ACCOUNT_COOLDOWN_SECS)
            self._consecutive_failures[normalized] += 1
            return self._consecutive_failures[normalized]

    def mark_rate_limited(self, index: int, retry_after: int | None) -> None:
        if not self.accounts:
            return
        seconds = (
            min(retry_after, RATE_LIMIT_COOLDOWN_MAX_SECS)
            if retry_after is not None and retry_after >= 0
            else RATE_LIMIT_COOLDOWN_SECS
        )
        with self._state_lock:
            self._set_cooldown(index, seconds)

    def mark_checkpointed(self, index: int) -> int:
        if not self.accounts:
            return 0
        normalized = index % len(self.accounts)
        with self._state_lock:
            self._set_cooldown(normalized, CHECKPOINT_COOLDOWN_SECS)
            self._consecutive_failures[normalized] += 1
            return self._consecutive_failures[normalized]

    def mark_ok(self, index: int) -> None:
        if not self.accounts:
            return
        normalized = index % len(self.accounts)
        with self._state_lock:
            self._cooldown_until[normalized] = 0
            self._consecutive_failures[normalized] = 0

    def reset_failure_count(self, index: int) -> None:
        if not self.accounts:
            return
        with self._state_lock:
            self._consecutive_failures[index % len(self.accounts)] = 0

    def in_cooldown(self, index: int) -> bool:
        if not self.accounts:
            return False
        normalized = index % len(self.accounts)
        return time.time() < self._cooldown_until[normalized]

    def affinity_for(self, key: str) -> int | None:
        with self._state_lock:
            return self._affinity.get(key)

    def set_affinity(self, key: str, account_index: int) -> None:
        if not self.accounts:
            return
        with self._state_lock:
            if len(self._affinity) >= AFFINITY_CAP and key not in self._affinity:
                self._affinity.pop(next(iter(self._affinity)))
            self._affinity[key] = account_index % len(self.accounts)

    def forget_affinity(self, key: str) -> None:
        with self._state_lock:
            self._affinity.pop(key, None)

    def account_order(self, affinity_key: str | None = None) -> list[int]:
        now = time.time()
        with self._state_lock:
            healthy = [
                index
                for index, cooldown_until in enumerate(self._cooldown_until)
                if now >= cooldown_until
            ]
            cooled = [
                index
                for index, cooldown_until in enumerate(self._cooldown_until)
                if now < cooldown_until
            ]
            preferred = self._affinity.get(affinity_key) if affinity_key else None
            if preferred in healthy:
                healthy.remove(preferred)
                healthy.insert(0, preferred)
            return [*healthy, *cooled]


class CookieStore:
    def __init__(self, jar: CookieJar | None = None) -> None:
        self._jar = jar or CookieJar()
        self._lock = threading.Lock()

    def snapshot(self) -> CookieJar:
        with self._lock:
            return self._jar

    def replace(self, jar: CookieJar) -> None:
        with self._lock:
            self._jar = jar


cookie_store = CookieStore()


def reload_cookie_store(
    cookie_path: Path | str,
    store: CookieStore = cookie_store,
) -> bool:
    try:
        new_jar = CookieJar.load_strict(cookie_path)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        logging.warning('cookie reload failed: %s', exc)
        return False
    store.replace(new_jar)
    logging.info('reloaded %s cookie account(s)', new_jar.len())
    return True


class CookieReloadController:
    _STOP = object()

    def __init__(self, cookie_path: Path | str, store: CookieStore) -> None:
        self.cookie_path = cookie_path
        self.store = store
        self._requests = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run,
            name='facebed-cookie-reload',
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def request_reload(self) -> None:
        self._requests.put(None)

    def stop(self) -> None:
        self._requests.put(self._STOP)

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is self._STOP:
                return
            reload_cookie_store(self.cookie_path, self.store)


_cookie_reload_controller_lock = threading.Lock()
_cookie_reload_controller: CookieReloadController | None = None


def install_cookie_reload_handler(
    cookie_path: Path | str,
    store: CookieStore = cookie_store,
) -> bool:
    global _cookie_reload_controller

    if not hasattr(signal, 'SIGHUP'):
        return False

    controller = CookieReloadController(cookie_path, store)

    def reload_handler(_signum, _frame) -> None:
        controller.request_reload()

    signal.signal(signal.SIGHUP, reload_handler)
    controller.start()
    with _cookie_reload_controller_lock:
        previous = _cookie_reload_controller
        _cookie_reload_controller = controller
    if previous is not None:
        previous.stop()
    return True


@dataclass(frozen=True)
class CookieAccountCheck:
    index: int
    label: str
    ok: bool
    account_name: str | None
    status: int | None
    reason: str | None


class NoCookies:
    @staticmethod
    def get_cookies() -> dict[str, str]:
        return {}


acc = NoCookies()


class Story:
    author_name: str
    text: str
    image_links: list[str]
    video_links: list[str]
    url: str

    author_id: int
    attached_story: Self

    def __init__(self, story_json: dict):
        if 'actors' in story_json or ('node_v2' not in story_json and ('comet_sections' in story_json or 'creation_story' in story_json or 'feedback' in story_json or 'attachments' in story_json)):
            node_v2 = story_json
        else:
            node_v2 = Jq.first(story_json, 'node_v2')
        if not isinstance(node_v2, dict):
            node_v2 = {}

        self.author_name = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'name' in story_json['actors'][0]:
            self.author_name = story_json['actors'][0]['name']
        elif node_v2.get('actors') and isinstance(node_v2['actors'], list) and len(node_v2['actors']) > 0 and 'name' in node_v2['actors'][0]:
            self.author_name = node_v2['actors'][0]['name']
        elif node_v2.get('name') and isinstance(node_v2['name'], str) and len(node_v2['name']) > 2:
            self.author_name = node_v2['name']
        elif node_v2.get('short_name'):
            self.author_name = node_v2['short_name']
        elif story_json.get('name') and isinstance(story_json['name'], str) and len(story_json['name']) > 2:
            self.author_name = story_json['name']
        else:
            self.author_name = Jq.first(story_json, 'name') or Jq.first(story_json, 'localized_name') or ''

        self.text = ''
        if 'message' in story_json and story_json['message'] and 'text' in story_json['message']:
            self.text = story_json['message']['text']
        elif story_json.get('text') and isinstance(story_json['text'], str):
            self.text = story_json['text']
        else:
            self.text = Jq.first(story_json, 'text') or ''

        self.image_links = self.get_image_links_post_json(story_json)
        self.video_links = self.get_video_links(story_json)

        self.url = story_json.get('wwwURL') or node_v2.get('wwwURL') or story_json.get('url') or ''
        if not isinstance(self.url, str):
            self.url = ''

        self.author_id = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'id' in story_json['actors'][0]:
            self.author_id = story_json['actors'][0]['id']
        elif node_v2.get('actors') and isinstance(node_v2['actors'], list) and len(node_v2['actors']) > 0 and 'id' in node_v2['actors'][0]:
            self.author_id = node_v2['actors'][0]['id']
        elif node_v2.get('id'):
            self.author_id = node_v2['id']
        else:
            self.author_id = Jq.first(story_json, 'id') or ''

        if 'attached_story' in story_json and story_json['attached_story'] and 'actors' in story_json['attached_story']:
            self.attached_story = Story(story_json['attached_story'])
            self.image_links.extend([x for x in self.attached_story.image_links if x not in self.image_links])
            self.video_links.extend([x for x in self.attached_story.video_links if x not in self.video_links])
        else:
            self.attached_story = None

    # TODO: find better format for this
    def get_text(self) -> str:
        text = self.text
        if self.attached_story:
            text += f'\n╰┈➤ {self.attached_story.author_name}\n{self.attached_story.text}'
        return text

    @staticmethod
    def get_video_links(post_json: dict) -> list[str]:
        video_links = []
        for attachment_set in Jq.all(post_json, 'attachment'):
            try:
                link = ReelsParser.get_video_link(None, user_node=attachment_set)
                if link not in video_links:
                    video_links.append(link)
            except FacebedException:
                pass

        return video_links

    @staticmethod
    def get_image_links_post_json(post_json: dict) -> list[str]:
        all_attachments = Jq.all(post_json, 'attachment')
        for attachment_set in all_attachments:
            if any([k.endswith('subattachments') for k in attachment_set]):
                subsets = [v for k, v in attachment_set.items() if k.endswith('subattachments') and 'nodes' in v]
                if subsets:
                    max_imgage_count = len(max(subsets, key=lambda it: len(it['nodes']))['nodes'])
                    subsets = [subset for subset in subsets if
                               len(subset['nodes']) == max_imgage_count and Jq.all(subset, 'viewer_image')]
                    if subsets:
                        images = [x['uri'] for x in Jq.all(subsets[0], 'viewer_image')]
                        if images:
                            return images
            elif 'media' in attachment_set and "'__typename': 'Sticker'" not in str(attachment_set):
                simplet_set = [x['uri'] for x in Jq.all(attachment_set, 'photo_image')]
                if simplet_set:
                    return simplet_set
        one_img = Story.fallback_get_image_link(post_json)
        if one_img:
            return [one_img]
        return []

    # facebook broke the original selector for all single-image posts, circa 10/12/2024
    @staticmethod
    def fallback_get_image_link(post_json: dict) -> str:
        for aa in Jq.all(post_json, 'comet_photo_attachment_resolution_renderer'):
            return aa['image']['uri']
        return ''

@dataclass
class ParsedPost:
    author_name: str
    text: str
    image_links: list[str]
    url: str
    date: int

    likes: str
    comments: str
    shares: str
    video_links: list[str]
    top_reaction_ids: tuple[str, ...] = ()


def banned(url: str) -> ParsedPost:
    Utils.warn(f'banned embed attempted "{url}"')
    return ParsedPost('Banned', 'This user is banned by the operators of this embed server',
                      [], 'https://banned.facebook.com', -1,
                      'null', 'null', 'null', [])


class JsonParser:
    @staticmethod
    def get_headers() -> dict:
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'priority': 'u=0, i',
            'sec-ch-prefers-color-scheme': 'dark',
            'sec-ch-ua': '"Not)A;Brand";v="8", "Chromium";v="146"',
            'sec-ch-ua-full-version-list': '"Not)A;Brand";v="8.0.0.0", "Chromium";v="146.0.7680.80"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-model': '""',
            'sec-ch-ua-platform': '"Windows"',
            'sec-ch-ua-platform-version': '"19.0.0"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
            'sec-fetch-user': '?1',
            'sec-gpc': '1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
        }

        return headers

    @staticmethod
    def get_json_blocks(html_parser: BeautifulSoup) -> list[dict]:
        script_elements = html_parser.find_all('script', attrs={'type': 'application/json'})
        
        data_blocks = []
        for e in script_elements:
            try:
                if e.text:
                    data = json.loads(e.text)
                    data_blocks.append(data)
            except json.JSONDecodeError:
                continue
        return data_blocks

    @staticmethod
    def get_requested_ids(post_path: str) -> list[str]:
        parsed = urlparse(JsonParser.ensure_full_url(post_path))
        requested_ids = []
        query = parse_qs(parsed.query)
        for key in ('story_fbid', 'fbid', 'v', 'multi_permalinks', 'post_id'):
            requested_ids.extend(query.get(key, []))

        posts_match = re.search(r'/posts/(.+)', parsed.path, re.IGNORECASE)
        if posts_match:
            post_parts = [part for part in posts_match.group(1).split('/') if part]
            post_id = next(
                (part for part in post_parts if part.lower().startswith('pfbid')),
                None,
            )
            if post_id is None:
                post_id = next(
                    (part for part in reversed(post_parts) if part.isdigit()),
                    post_parts[0] if post_parts else None,
                )
            if post_id:
                requested_ids.append(post_id)

        for pattern in (
            r'/reel/([^/?]+)',
            r'/videos/(?:pcb\.\d+/)?([^/?]+)',
            r'/watch/([^/?]+)',
            r'(?<!share)/v/(\d+)(?:/|$)',
            r'/permalink/([^/?]+)',
            r'/photos/(?:[^/]+/)?([^/?]+)/?$',
        ):
            match = re.search(pattern, parsed.path, re.IGNORECASE)
            if match:
                requested_ids.append(match.group(1))
        return list(dict.fromkeys(value for value in requested_ids if value))

    @staticmethod
    def get_target_ids(
        html_parser: BeautifulSoup,
        post_path: str,
    ) -> tuple[list[str], list[str]]:
        raw_ids = JsonParser.get_requested_ids(post_path)
        route_proven_ids: list[str] = []
        source_is_share = Utils.is_share_path(post_path)

        def add(value, target: list[str]) -> None:
            if isinstance(value, (str, int)) and str(value) and str(value) not in target:
                target.append(str(value))

        def share_key(value: str) -> str:
            normalized = Utils.normalize_facebook_path(value)
            return urlparse(JsonParser.ensure_full_url(normalized)).path.rstrip('/')

        source_share_key = share_key(post_path) if source_is_share else ''

        for block in JsonParser.get_json_blocks(html_parser):
            route_info = Jq.first(block, 'initialRouteInfo')
            route = route_info.get('route') if isinstance(route_info, dict) else None
            if not isinstance(route, dict):
                continue
            route_name = str(route.get('canonicalRouteName', '')).lower()
            if not any(marker in route_name for marker in ('post', 'video', 'reel', 'photo')):
                continue
            route_ids = JsonParser.get_requested_ids(str(route.get('url', '')))

            candidate_ids: list[str] = []
            for value in route_ids:
                add(value, candidate_ids)

            params = route.get('params')
            route_share_keys: list[str] = []
            if isinstance(params, dict):
                for key in ('story_token', 'story_fbid', 'fbid', 'video_id', 'v', 'post_id'):
                    add(params.get(key), candidate_ids)
                if params.get('share_url'):
                    route_share_keys.append(share_key(str(params['share_url'])))

            route_url_query = parse_qs(urlparse(str(route.get('url', ''))).query)
            for share_url in route_url_query.get('share_url', []):
                route_share_keys.append(share_key(str(share_url)))

            for view_key in ('rootView', 'hostableView'):
                view = route.get(view_key)
                props = view.get('props') if isinstance(view, dict) else None
                if not isinstance(props, dict):
                    continue
                for key in ('storyID', 'videoID', 'postID'):
                    add(props.get(key), candidate_ids)

            route_matches_request = bool(
                raw_ids and set(raw_ids).intersection(candidate_ids)
            )
            opaque_share_target = bool(
                source_is_share
                and not raw_ids
                and candidate_ids
                and source_share_key in route_share_keys
            )
            if not route_matches_request and not opaque_share_target:
                continue
            for value in candidate_ids:
                add(value, route_proven_ids)

        return (
            list(dict.fromkeys([*route_proven_ids, *raw_ids])),
            route_proven_ids,
        )

    @staticmethod
    def contains_exact_id(value, requested_ids: list[str]) -> bool:
        if not requested_ids:
            return False
        if isinstance(value, dict):
            id_fields = {
                'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
                'storyfbid', 'story_id', 'storyid', 'fbid', 'legacy_fbid', 'top_level_post_id',
                'mf_story_key', 'feedback_id',
            }
            for key, item in value.items():
                if str(key).lower() in id_fields and isinstance(item, (str, int)):
                    if str(item) in requested_ids:
                        return True
                if str(key).lower() in id_fields and isinstance(item, list):
                    if any(
                        isinstance(part, (str, int)) and str(part) in requested_ids
                        for part in item
                    ):
                        return True
                if JsonParser.contains_exact_id(item, requested_ids):
                    return True
        elif isinstance(value, list):
            return any(JsonParser.contains_exact_id(item, requested_ids) for item in value)
        return False

    @staticmethod
    def contains_target_id(value, requested_ids: list[str]) -> bool:
        if not requested_ids:
            return False
        if isinstance(value, dict):
            identity_urls = {
                'url', 'wwwurl', 'permalink', 'permalink_url',
                'shareable_url', 'canonical_url',
            }
            for key, item in value.items():
                if str(key).lower() in identity_urls and isinstance(item, str):
                    if set(JsonParser.get_requested_ids(item)).intersection(requested_ids):
                        return True
                if JsonParser.contains_target_id(item, requested_ids):
                    return True
            return JsonParser.contains_exact_id(value, requested_ids)
        if isinstance(value, list):
            return any(JsonParser.contains_target_id(item, requested_ids) for item in value)
        return False

    @staticmethod
    def select_requested_candidate(candidates, requested_ids: list[str]):
        candidates = [candidate for candidate in candidates if isinstance(candidate, dict)]
        for candidate in candidates:
            if JsonParser.contains_exact_id(candidate, requested_ids):
                return candidate
        for candidate in candidates:
            if JsonParser.contains_target_id(candidate, requested_ids):
                return candidate
        if requested_ids:
            return None
        return candidates[0] if candidates else None

    @staticmethod
    def select_requested_field(blocks: list[dict], field_name: str, requested_ids: list[str]):
        matching = []
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if field_name not in node:
                    continue
                if JsonParser.contains_exact_id(node, requested_ids):
                    matching.append((node, node[field_name]))
                elif JsonParser.contains_target_id(node, requested_ids):
                    matching.append((node, node[field_name]))
        if matching:
            return min(matching, key=lambda item: len(str(item[0])))[1]
        if requested_ids:
            return None
        for block in blocks:
            value = Jq.first(block, field_name)
            if value not in (None, [], ''):
                return value
        return None

    @staticmethod
    def probe_page_type(html_parser: BeautifulSoup, post_path: str = '') -> str:
        blocks = JsonParser.get_json_blocks(html_parser)
        initial_routes = []
        for block in blocks:
            route_info = Jq.first(block, 'initialRouteInfo')
            if isinstance(route_info, dict) and isinstance(route_info.get('route'), dict):
                initial_routes.append(route_info['route'])
        if any(
            route.get('canonicalRouteName')
            == 'comet.fbweb.CometVideoHomeVideoNotFoundRoute'
            for route in initial_routes
        ):
            return 'no_data'

        if any(
            route.get('canonicalRouteName')
            == 'comet.fbweb.CometProfilePlusLoggedOutRoute'
            for route in initial_routes
        ):
            return 'no_data'

        has_profile_app_link = any(
            str(meta.get('property', '')).lower() in {'al:android:url', 'al:ios:url'}
            and str(meta.get('content', '')).lower().startswith('fb://profile/')
            for meta in html_parser.find_all('meta')
        )
        if has_profile_app_link:
            canonical = html_parser.find('link', attrs={'rel': 'canonical'})
            canonical_path = urlparse(canonical.get('href', '')).path if canonical else ''
            path = canonical_path or urlparse(JsonParser.ensure_full_url(post_path)).path
            post_like = bool(re.search(
                r'/(?:posts|permalink\.php|story\.php|photo(?:\.php)?|reel|watch|videos|v)(?:/|$)',
                path,
                re.IGNORECASE,
            ))
            profile_root = bool(
                re.match(r'^/people/[^/]+/\d+/?$', path, re.IGNORECASE)
                or (
                    len([part for part in path.split('/') if part]) == 1
                    and not post_like
                )
            )
            profile_target_ids, profile_route_ids = JsonParser.get_target_ids(
                html_parser, post_path
            )
            profile_required_ids = (
                profile_route_ids
                or JsonParser.get_requested_ids(post_path)
                or profile_target_ids
            )
            has_target_post_data = bool(profile_required_ids) and any(
                JsonParser.contains_exact_id(block, profile_required_ids)
                and any(
                    Jq.has(block, marker)
                    for marker in (
                        'i18n_reaction_count',
                        'short_form_video_context',
                        'message_preferred_body',
                        'prefetch_uris_v2',
                        'attached_comment',
                        'comment_rendering_instance',
                        'creation_story',
                        'browser_native_hd_url',
                        'browser_native_sd_url',
                    )
                )
                for block in blocks
            )
            if profile_root and not has_target_post_data:
                return 'no_data'

        has_generic_data = any(
            Jq.has(block, 'i18n_reaction_count') or Jq.has(block, 'short_form_video_context')
            for block in blocks
        )
        has_photo_data = (
            any(Jq.has(block, 'message_preferred_body', 'container_story') for block in blocks)
            and any(Jq.has(block, 'prefetch_uris_v2') for block in blocks)
        )
        has_photocom_data = (
            any(Jq.has(block, 'attached_comment') and not Jq.has(block, 'unified_reactors') for block in blocks)
            and any(Jq.has(block, 'attached_comment', 'unified_reactors') for block in blocks)
        )
        has_watch_data = any(
            Jq.has(block, 'comment_rendering_instance', 'video_view_count_renderer')
            for block in blocks
        )
        has_reel_data = (
            any(Jq.has(block, 'creation_story') or Jq.has(block, 'short_form_video_context') for block in blocks)
            and any(
                Jq.has(block, 'browser_native_hd_url') or Jq.has(block, 'browser_native_sd_url')
                for block in blocks
            )
        )
        parsed_url = urlparse(JsonParser.ensure_full_url(post_path))
        parsed_path = parsed_url.path
        is_photo_route = bool(re.match(r'^/?photo(?:\.php)?/?$', parsed_path, re.IGNORECASE))
        is_watch_route = bool(re.match(r'^/?watch(?:/|$)', parsed_path, re.IGNORECASE))
        is_reel_route = bool(
            re.match(r'^/?reel(?:/|$)', parsed_path, re.IGNORECASE)
            or re.search(r'(?:^|/)videos/', parsed_path, re.IGNORECASE)
            or re.search(r'(?:^|/)(?:[^/]+/)?v/\d+(?:/|$)', parsed_path, re.IGNORECASE)
        )
        is_photocom_route = '3' in parse_qs(parsed_url.query).get('type', [])

        def block_has_route_data(block: dict) -> bool:
            if Jq.has(block, 'i18n_reaction_count') or Jq.has(block, 'short_form_video_context'):
                return True
            if is_photo_route and (
                Jq.has(block, 'message_preferred_body', 'container_story')
                or Jq.has(block, 'prefetch_uris_v2')
            ):
                return True
            if is_photocom_route and Jq.has(block, 'attached_comment'):
                return True
            if is_watch_route and Jq.has(
                block, 'comment_rendering_instance', 'video_view_count_renderer'
            ):
                return True
            if is_reel_route and (
                Jq.has(block, 'creation_story')
                or Jq.has(block, 'browser_native_hd_url')
                or Jq.has(block, 'browser_native_sd_url')
            ):
                return True
            return False

        def has_requested_route_data(requested_ids: list[str]) -> bool:
            return bool(requested_ids) and any(
                block_has_route_data(block)
                and JsonParser.contains_exact_id(block, requested_ids)
                for block in blocks
            )

        requested_ids = JsonParser.get_requested_ids(post_path)
        has_route_data = has_generic_data
        if is_photo_route:
            has_route_data = has_route_data or has_photo_data
        if is_photocom_route:
            has_route_data = has_route_data or has_photocom_data
        if is_watch_route:
            has_route_data = has_route_data or has_watch_data
        if is_reel_route:
            has_route_data = has_route_data or has_reel_data
        if Utils.is_share_path(post_path):
            has_route_data = (
                has_route_data
                or has_photo_data
                or has_photocom_data
                or has_watch_data
                or has_reel_data
            )
        canonical = html_parser.find('link', attrs={'rel': 'canonical'})
        if canonical and urlparse(canonical.get('href', '')).path.rstrip('/') == '/watch':
            if has_requested_route_data(requested_ids):
                return 'has_data'
            if Utils.is_share_path(post_path) and has_route_data:
                return 'has_data'
            return 'no_data'

        if canonical and re.search(r'/login\b', canonical.get('href', '')):
            if has_requested_route_data(requested_ids):
                return 'has_data'
            return 'login_wall'

        if has_route_data:
            return 'has_data'
        
        return 'no_data'

    @staticmethod
    def check_page_or_raise(html_parser: BeautifulSoup, post_path: str):
        page_type = JsonParser.probe_page_type(html_parser, post_path)
        if page_type in ('login_wall', 'no_data'):
            raise NoDataException(f'Facebook served a login wall or empty page for {post_path} - content requires authentication')

    @staticmethod
    @contextmanager
    def fetch_page(
        post_path: str,
        use_cookies: bool = True,
        http_response: CffiResponse | None = None,
    ):
        url = JsonParser.ensure_full_url(post_path)
        owns_response = http_response is None
        if owns_response:
            http_response = cffi.get(
                url,
                _check_status=False,
                _retry_status_responses=False,
            )

        def prepare_page(current_response: CffiResponse):
            cffi.select_response(current_response)
            current_html = current_response.text
            current_parser = BeautifulSoup(current_html, 'html.parser')
            JsonParser.check_page_or_raise(current_parser, post_path)
            return current_html, current_parser

        try:
            raw_html, html_parser = prepare_page(http_response)
        except NoDataException:
            if owns_response and http_response.status_code in cffi.retry_statuses:
                http_response = cffi.get(
                    url,
                    _check_status=False,
                    _retry_status_responses=False,
                    _bypass_cache=True,
                )
                try:
                    raw_html, html_parser = prepare_page(http_response)
                except NoDataException:
                    if http_response.status_code >= 400:
                        raise UpstreamException(
                            f'Facebook returned HTTP {http_response.status_code} for {url}',
                            http_response,
                        )
                    raise
            elif http_response.status_code >= 400:
                raise UpstreamException(
                    f'Facebook returned HTTP {http_response.status_code} for {url}',
                    http_response,
                )
            else:
                raise
        try:
            yield html_parser
        except ParseException as e:
            if not e.html:
                e.html = raw_html
                e.url = url
            raise

    @staticmethod
    def get_post_json(
        html_parser: BeautifulSoup,
        post_path: str = '',
        requested_ids: list[str] | None = None,
        required_ids: list[str] | None = None,
    ) -> dict:
        candidate_blocks = []
        blocks = JsonParser.get_json_blocks(html_parser)
        for bloc in blocks:
            if Jq.has(bloc, 'i18n_reaction_count') or Jq.has(bloc, 'short_form_video_context'):
                candidate_blocks.append(bloc)

        if not candidate_blocks:
            for bloc in blocks:
                if 'video' in str(bloc) and 'short_form_video_context' in str(bloc):
                    candidate_blocks.append(bloc)
                    
        if not candidate_blocks:
            raise ParseException('cannot find post json')

        def score_block(bloc: dict) -> int:
            score = 0
            if Jq.has(bloc, 'short_form_video_context'):
                score += 20
            if Jq.has(bloc, 'creation_story'):
                score += 10
            if Jq.has(bloc, 'comet_sections'):
                score += 5
            if Jq.has(bloc, 'group_hoisted_feed'):
                score += 8
            if Jq.has(bloc, 'video_home_www_related_videos_section') or Jq.has(bloc, 'video_home_www_loe_video_permalink_seo_info'):
                score -= 20
            node_v2 = Jq.first(bloc, 'node_v2')
            if isinstance(node_v2, dict):
                if 'actors' in node_v2 and node_v2['actors']:
                    score += 30
                if 'feedback' in node_v2 and isinstance(node_v2['feedback'], dict):
                    score += 15
                if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                    score += 10
            data_blob = Jq.first(bloc, 'data')
            if isinstance(data_blob, dict):
                if 'actors' in data_blob and data_blob['actors']:
                    score += 20
                if 'feedback' in data_blob and isinstance(data_blob['feedback'], dict):
                    score += 10
            if 'require' in bloc and not node_v2 and not data_blob:
                score -= 10
            return score

        requested_ids = (
            JsonParser.get_requested_ids(post_path)
            if requested_ids is None else requested_ids
        )
        required_ids = required_ids or []
        candidate_blocks.sort(
            key=lambda block: (
                JsonParser.contains_target_id(block, required_ids),
                JsonParser.contains_exact_id(block, requested_ids),
                JsonParser.contains_target_id(block, requested_ids),
                score_block(block),
            ),
            reverse=True,
        )
        for candidate in candidate_blocks:
            try:
                root = JsonParser.get_root_node(candidate, requested_ids)
                if required_ids and not JsonParser.contains_target_id(root, required_ids):
                    continue
                return candidate
            except ParseException:
                continue
        if required_ids:
            raise NoDataException('Facebook response did not contain the requested post')
        return candidate_blocks[0]

    @staticmethod
    def get_group_name(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> str:
        candidates = [
            bloc for bloc in JsonParser.get_json_blocks(html_parser)
            if Jq.has(bloc, 'group_member_profiles', 'formatted_count_text')
        ]
        requested_ids = requested_ids or []
        selected = JsonParser.select_requested_candidate(candidates, requested_ids)
        if selected is None:
            return ''
        for group_object in Jq.all(selected, 'group'):
            if isinstance(group_object, dict) and 'name' in group_object:
                return group_object['name']
        return ''

    @staticmethod
    def get_interaction_counts(
        post_json: dict,
        requested_ids: list[str] | None = None,
        top_reaction_ids: list[str] | None = None,
    ) -> tuple[str, str, str]:
        assert post_json
        if top_reaction_ids is not None:
            top_reaction_ids.clear()

        def extract_counts(fb: dict) -> tuple[str, str, str]:
            if top_reaction_ids is not None:
                top_reaction_ids.extend(Utils.get_top_reaction_ids(fb))
            reaction_count = fb.get('reaction_count')
            if isinstance(reaction_count, dict):
                reaction_count = reaction_count.get('count')
            share_count = fb.get('share_count')
            if isinstance(share_count, dict):
                share_count = share_count.get('count')
            reactions = fb.get('i18n_reaction_count') or reaction_count or Jq.first(fb, 'i18n_reaction_count') or '0'
            shares = fb.get('i18n_share_count') or share_count or Jq.first(fb, 'i18n_share_count') or Jq.first(fb, 'share_count') or '0'
            comments = fb.get('total_comment_count') or Jq.first(fb, 'total_comment_count')
            if not comments:
                cri = fb.get('comment_rendering_instance')
                if isinstance(cri, dict):
                    cnode = cri.get('comments')
                    if isinstance(cnode, dict):
                        comments = cnode.get('total_count')
                if not comments:
                    ccsr = fb.get('comments_count_summary_renderer')
                    if isinstance(ccsr, dict):
                        fb_inner = ccsr.get('feedback')
                        if isinstance(fb_inner, dict):
                            cri2 = fb_inner.get('comment_rendering_instance')
                            if isinstance(cri2, dict):
                                cnode2 = cri2.get('comments')
                                if isinstance(cnode2, dict):
                                    comments = cnode2.get('total_count')
            if not comments:
                comments = '0'
            return str(reactions), str(comments), str(shares)

        def best_feedback() -> dict | None:
            best = None
            best_reactions = 0
            for fb in Jq.all(post_json, 'feedback'):
                if isinstance(fb, dict):
                    rc = fb.get('i18n_reaction_count')
                    if rc:
                        try:
                            n = int(rc)
                            if n > best_reactions:
                                best_reactions = n
                                best = fb
                        except (ValueError, TypeError):
                            pass
                    elif best is None:
                        best = fb
            return best

        requested_ids = [str(value) for value in (requested_ids or []) if value]
        id_keys = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
            'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
            'top_level_post_id', 'mf_story_key', 'feedback_id',
        }
        direct_feedback = post_json.get('feedback')
        for identity_source in (post_json, direct_feedback):
            if not isinstance(identity_source, dict):
                continue
            for key, item in identity_source.items():
                if str(key).lower() not in id_keys:
                    continue
                if isinstance(item, (str, int)) and str(item):
                    requested_ids.append(str(item))
                elif isinstance(item, list):
                    requested_ids.extend(
                        str(part) for part in item
                        if isinstance(part, (str, int)) and str(part)
                    )
        requested_ids = list(dict.fromkeys(requested_ids))
        if requested_ids:
            contextual_feedbacks: list[dict] = []

            def walk(value, inherited_ids: set[str] | None = None) -> None:
                inherited_ids = inherited_ids or set()
                if isinstance(value, dict):
                    own_ids: set[str] = set()
                    for key, item in value.items():
                        if (
                            str(key).lower() not in id_keys
                            or not isinstance(item, (str, int))
                        ):
                            continue
                        text = str(item)
                        own_ids.add(text)
                        try:
                            decoded = base64.b64decode(
                                text + ('=' * (-len(text) % 4)),
                                validate=True,
                            ).decode('utf-8')
                        except (ValueError, UnicodeDecodeError):
                            continue
                        for requested_id in requested_ids:
                            if requested_id == decoded or (
                                requested_id.isdigit()
                                and re.search(
                                    rf'(?<![A-Za-z0-9]){re.escape(requested_id)}(?![A-Za-z0-9])',
                                    decoded,
                                )
                            ):
                                own_ids.add(requested_id)
                    context_ids = own_ids or inherited_ids
                    feedback = value.get('feedback')
                    if (
                        isinstance(feedback, dict)
                        and context_ids.intersection(requested_ids)
                    ):
                        contextual_feedbacks.append(feedback)
                    for item in value.values():
                        walk(item, context_ids)
                elif isinstance(value, list):
                    for item in value:
                        walk(item, inherited_ids)

            walk(post_json)
            if isinstance(direct_feedback, dict) and any(
                marker in direct_feedback
                for marker in (
                    'i18n_reaction_count', 'reaction_count', 'total_comment_count',
                    'i18n_share_count', 'share_count',
                    'comment_rendering_instance', 'comments_count_summary_renderer',
                )
            ):
                return extract_counts(direct_feedback)
            if contextual_feedbacks:
                def reaction_score(feedback: dict) -> float:
                    rc = feedback.get('reaction_count')
                    if isinstance(rc, dict):
                        rc = rc.get('count')
                    if rc is None:
                        rc = feedback.get('i18n_reaction_count')
                    if rc is None:
                        return -1
                    text = str(rc).strip().upper().replace(',', '')
                    multiplier = 1
                    if text.endswith(('K', 'M', 'B')):
                        multiplier = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}[text[-1]]
                        text = text[:-1]
                    try:
                        return float(text) * multiplier
                    except (TypeError, ValueError):
                        return 0

                best = max(
                    contextual_feedbacks,
                    key=reaction_score,
                )
                return extract_counts(best)
            return '0', '0', '0'

        post_feedback = Jq.first(post_json, 'comet_ufi_summary_and_actions_renderer')
        if post_feedback and isinstance(post_feedback, dict) and 'feedback' in post_feedback:
            return extract_counts(post_feedback['feedback'])

        fb = Jq.first(post_json, 'feedback')
        if fb and isinstance(fb, dict):
            rc = fb.get('i18n_reaction_count')
            if rc:
                return extract_counts(fb)
            best = best_feedback()
            if best:
                return extract_counts(best)

        best = best_feedback()
        if best:
            return extract_counts(best)

        reactions = Jq.first(post_json, 'i18n_reaction_count') or '0'
        shares = Jq.first(post_json, 'i18n_share_count') or Jq.first(post_json, 'share_count') or '0'
        comments = Jq.first(post_json, 'total_comment_count')
        if not comments:
            cri = Jq.first(post_json, 'comment_rendering_instance')
            if isinstance(cri, dict):
                cnode = cri.get('comments')
                if isinstance(cnode, dict):
                    comments = cnode.get('total_count')
            if not comments:
                ccsr = Jq.first(post_json, 'comments_count_summary_renderer')
                if isinstance(ccsr, dict):
                    fb_inner = ccsr.get('feedback')
                    if isinstance(fb_inner, dict):
                        cri2 = fb_inner.get('comment_rendering_instance')
                        if isinstance(cri2, dict):
                            cnode2 = cri2.get('comments')
                            if isinstance(cnode2, dict):
                                comments = cnode2.get('total_count')
        if not comments:
            comments = '0'

        return str(reactions), str(comments), str(shares)

    @staticmethod
    def get_root_node(post_json: dict, requested_ids: list[str] | None = None) -> dict:
        requested_ids = requested_ids or []

        def selected(key: str, source: dict = post_json):
            return JsonParser.select_requested_candidate(Jq.all(source, key), requested_ids)

        def work_normal_post() -> dict:
            data_blob = selected('data')
            if not isinstance(data_blob, dict):
                short_form = selected('short_form_video_context')
                if short_form:
                    return {'creation_story': short_form}
                return {}
            if 'comet_ufi_summary_and_actions_renderer' in data_blob:
                return data_blob
            elif 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
                node_v2 = data_blob['node_v2']
                if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                    return node_v2
            elif 'node' in data_blob and isinstance(data_blob['node'], dict):
                node = data_blob['node']
                if 'comet_sections' in node or 'creation_story' in node:
                    return node
            short_form = selected('short_form_video_context', data_blob)
            if short_form:
                return {'creation_story': short_form}
            return {}

        def work_group_post() -> dict:
            hoisted_feed = selected('group_hoisted_feed')
            if isinstance(hoisted_feed, dict):
                if 'comet_sections' in hoisted_feed or 'creation_story' in hoisted_feed:
                    return hoisted_feed
                node_v2 = selected('node_v2', hoisted_feed)
                if isinstance(node_v2, dict):
                    return node_v2

            data_blob = selected('data')
            if isinstance(data_blob, dict):
                group = data_blob.get('group')
                if isinstance(group, dict):
                    if 'comet_sections' in group or 'creation_story' in group:
                        return group
                    node_v2 = selected('node_v2', group)
                    if isinstance(node_v2, dict):
                        return node_v2
            return {}

        methods: list[Callable[[], dict]] = [work_normal_post, work_group_post]

        for method in methods:
            try:
                ret = method()
                if ret:
                    return ret
            except (StopIteration, KeyError):
                continue

        data_blob = selected('data')
        if isinstance(data_blob, dict):
            if 'creation_story' in data_blob and 'feedback' in data_blob:
                return data_blob
            if 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
                return data_blob['node_v2']

        raise ParseException('Cannot process post')

    @staticmethod
    def ensure_full_url(u: str) -> str:
        value = str(u)
        parsed = urlparse(value)
        if not parsed.netloc and re.match(r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE):
            parsed = urlparse(f'https://{value}')
        if parsed.netloc:
            hostname = (parsed.hostname or '').lower()
            if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
                suffix = parsed.path.lstrip('/')
                if parsed.query:
                    suffix += f'?{parsed.query}'
                return f'{WWWFB}/{suffix}'
            return value
        return f'{WWWFB}/{value.removeprefix("/")}'

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            raw_ids = JsonParser.get_requested_ids(post_path)
            requested_ids, route_proven_ids = JsonParser.get_target_ids(html_parser, post_path)
            if Utils.is_share_path(post_path) and not raw_ids and not route_proven_ids:
                raise NoDataException(
                    'Facebook response did not identify the requested share post'
                )
            required_ids = route_proven_ids or raw_ids
            post_json = JsonParser.get_root_node(
                JsonParser.get_post_json(
                    html_parser,
                    post_path,
                    requested_ids,
                    required_ids,
                ),
                requested_ids,
            )
            if required_ids and not JsonParser.contains_target_id(post_json, required_ids):
                raise NoDataException('Facebook response selected a different post')
            top_reaction_ids: list[str] = []
            likes, cmts, shares = JsonParser.get_interaction_counts(
                post_json, requested_ids, top_reaction_ids
            )

            post_date = -1
            t = Jq.first(post_json, 'creation_time') or Jq.first(post_json, 'created_time')
            if t:
                try:
                    post_date = int(t)
                except (ValueError, TypeError):
                    pass
            if post_date == -1:
                blocks = JsonParser.get_json_blocks(html_parser)
                t = (
                    JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
                    or JsonParser.select_requested_field(blocks, 'created_time', requested_ids)
                )
                if t:
                    try:
                        post_date = int(t)
                    except (ValueError, TypeError):
                        pass

            story_dict = post_json
            if 'content' in post_json and isinstance(post_json['content'], dict) and 'story' in post_json['content']:
                story_dict = post_json['content']['story']
            elif 'creation_story' in post_json:
                story_dict = post_json['creation_story']
                if 'owner' in post_json and ('actors' not in story_dict or not story_dict['actors']):
                    story_dict['actors'] = [post_json['owner']]
            elif 'comet_sections' in post_json:
                sections = post_json['comet_sections']
                if isinstance(sections, dict) and 'content' in sections and isinstance(sections['content'], dict) and 'story' in sections['content']:
                    story_dict = sections['content']['story']
                elif 'feedback' in post_json:
                    pass

            story = Story(story_dict)
            parsed_post_path = urlparse(JsonParser.ensure_full_url(post_path)).path
            if not story.video_links and re.search(
                r'/(?:reel|watch|videos|v)(?:/|$)', parsed_post_path, re.IGNORECASE
            ):
                try:
                    story.video_links.append(
                        ReelsParser.get_video_link(
                            html_parser, requested_ids=requested_ids
                        )
                    )
                except FacebedException:
                    pass
            canonical = html_parser.find('link', attrs={'rel': 'canonical'})
            canonical_url = str(canonical.get('href', '')) if canonical else ''
            parsed_canonical = urlparse(canonical_url)
            canonical_host = (parsed_canonical.hostname or '').lower()
            validated_identity_ids = set(route_proven_ids or raw_ids)
            identity_fields = {
                'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
                'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
                'top_level_post_id', 'mf_story_key', 'feedback_id',
            }
            def collect_identity_ids(value) -> None:
                if not isinstance(value, dict):
                    return
                for key, item in value.items():
                    key_name = str(key).lower()
                    if key_name in identity_fields and isinstance(item, (str, int)):
                        validated_identity_ids.add(str(item))
                    elif key_name in identity_fields and isinstance(item, list):
                        validated_identity_ids.update(
                            str(part) for part in item
                            if isinstance(part, (str, int))
                        )

            collect_identity_ids(post_json)
            collect_identity_ids(story_dict)
            story_url_ids = set(JsonParser.get_requested_ids(story.url))
            canonical_ids = set(JsonParser.get_requested_ids(canonical_url))
            if (
                canonical_host == 'facebook.com'
                or canonical_host.endswith('.facebook.com')
            ) and canonical_ids.intersection(validated_identity_ids):
                parts = [part for part in parsed_canonical.path.split('/') if part]
                if 'posts' in parts:
                    post_index = parts.index('posts')
                    numeric_ids = [part for part in parts[post_index + 1:] if part.isdigit()]
                    if numeric_ids:
                        parts = [*parts[:post_index + 1], numeric_ids[-1]]
                        parsed_canonical = parsed_canonical._replace(
                            path='/' + '/'.join(parts),
                            query='',
                            fragment='',
                        )
                post_url = parsed_canonical.geturl()
            elif story.url and story_url_ids.intersection(validated_identity_ids):
                post_url = story.url or JsonParser.ensure_full_url(post_path)
            else:
                post_url = JsonParser.ensure_full_url(post_path)
            post_content = story.get_text()
            post_group_name = JsonParser.get_group_name(html_parser, requested_ids)
            post_author_name = story.author_name
            link_header = f'{post_author_name}' + (f' • {post_group_name}' if post_group_name else '')

            if story.author_id in config['banned_users']:
                return banned(post_url)

            # TODO: support normal /watch here
            return ParsedPost(link_header, post_content.strip(), story.image_links, post_url, post_date,
                              likes, cmts, shares, story.video_links,
                              top_reaction_ids=tuple(top_reaction_ids))


class SinglePhotoParser:
    @staticmethod
    def _select_node(
        candidates: list[dict],
        requested_ids: list[str],
        error_message: str,
    ) -> dict:
        ordered = (
            sorted(candidates, key=lambda item: len(str(item)))
            if requested_ids else candidates
        )
        selected = JsonParser.select_requested_candidate(ordered, requested_ids)
        if selected is not None:
            return selected
        if candidates and requested_ids:
            raise NoDataException('Facebook response did not contain the requested photo')
        raise ParseException(error_message)

    @staticmethod
    def get_content_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'message_preferred_body', 'container_story'):
                candidates.append(bloc)
        selected_block = SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'Cannot process post (cn)',
        )
        data_nodes = [
            node for node in Jq.all(selected_block, 'data') if isinstance(node, dict)
        ]
        selected_data = JsonParser.select_requested_candidate(
            data_nodes, requested_ids or []
        )
        if selected_data is not None:
            return selected_data
        if len(data_nodes) == 1 and not requested_ids:
            return data_nodes[0]
        if len(data_nodes) == 1 and JsonParser.contains_exact_id(
            selected_block, requested_ids or []
        ):
            return data_nodes[0]
        raise NoDataException('Facebook response did not contain the requested photo')

    @staticmethod
    def get_interactions_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict | None:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'comet_ufi_summary_and_actions_renderer'):
                candidates.append(bloc)
        try:
            return SinglePhotoParser._select_node(
                candidates,
                requested_ids or [],
                'Cannot process post (in)',
            )
        except NoDataException:
            if len(candidates) == 1:
                return None
            raise

    @staticmethod
    def get_single_image(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> str:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'prefetch_uris_v2'):
                candidates.append(bloc)
        selected_block = SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'cannot find single image',
        )
        prefetch = Jq.first(selected_block, 'prefetch_uris_v2')
        if not isinstance(prefetch, list) or not prefetch:
            raise ParseException('cannot find single image')
        return str(prefetch[0]['uri'])

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _route_proven_ids = JsonParser.get_target_ids(
                html_parser, post_path
            )
            content_node = SinglePhotoParser.get_content_node(html_parser, requested_ids)
            interaction_ids = list(requested_ids)
            identity_keys = {
                'id', 'video_id', 'post_id', 'story_fbid', 'fbid',
                'legacy_fbid', 'top_level_post_id', 'mf_story_key', 'feedback_id',
            }
            identity_sources = [content_node]
            for key in ('container_story', 'creation_story'):
                source = Jq.first(content_node, key)
                if isinstance(source, dict):
                    identity_sources.append(source)
            for source in identity_sources:
                for key, value in source.items():
                    if str(key).lower() in identity_keys and isinstance(value, (str, int)):
                        text = str(value)
                        interaction_ids.append(text)
                        try:
                            decoded = base64.b64decode(
                                text + ('=' * (-len(text) % 4)), validate=True
                            ).decode('utf-8')
                        except (ValueError, UnicodeDecodeError):
                            continue
                        numeric_parts = re.findall(r'\d+', decoded)
                        if numeric_parts:
                            interaction_ids.append(numeric_parts[-1])
            interaction_ids = list(dict.fromkeys(interaction_ids))
            interaction_node = SinglePhotoParser.get_interactions_node(
                html_parser, interaction_ids
            )

            post_text = content_node['message']['text'] if content_node['message'] and 'text' in content_node['message'] else ''
            post_author = content_node['owner']['name']
            post_date = content_node['created_time']
            if interaction_node is None:
                likes, cmts, shares = '0', '0', '0'
                top_reaction_ids: list[str] = []
            else:
                top_reaction_ids = []
                likes, cmts, shares = JsonParser.get_interaction_counts(
                    interaction_node, interaction_ids, top_reaction_ids
                )
            image_url = SinglePhotoParser.get_single_image(html_parser, requested_ids)

            return ParsedPost(post_author, post_text.strip(), [image_url], JsonParser.ensure_full_url(post_path),
                              post_date, likes, cmts, shares, [],
                              top_reaction_ids=tuple(top_reaction_ids))


class PhotocomParser:
    @staticmethod
    def get_content_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'attached_comment') and not Jq.has(bloc, 'unified_reactors'):
                candidates.extend(
                    node for node in Jq.all(bloc, 'result') if isinstance(node, dict)
                )
        return SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'Cannot process photocom (cn)',
        )

    @staticmethod
    def get_media_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict:
        candidates = [
            bloc for bloc in JsonParser.get_json_blocks(html_parser)
            if Jq.has(bloc, 'attached_comment', 'unified_reactors')
        ]
        return SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'Cannot process photocom (media)',
        )

    @staticmethod
    def get_reaction_count(media_node: dict) -> int:
        feedback = PhotocomParser.get_reaction_feedback(media_node)
        reactors = feedback.get('unified_reactors')
        if not isinstance(reactors, dict) or 'count' not in reactors:
            reactors = Jq.first(media_node, 'unified_reactors')
        if isinstance(reactors, dict) and 'count' in reactors:
            return reactors['count']
        raise ParseException('Cannot process photocom (rc)')

    @staticmethod
    def get_reaction_feedback(media_node: dict) -> dict:
        cur = Jq.first(media_node, 'currMedia')
        if isinstance(cur, dict):
            attached_comment = cur.get('attached_comment')
            if isinstance(attached_comment, dict):
                feedback = attached_comment.get('feedback')
                if isinstance(feedback, dict):
                    return feedback
        raise ParseException('Cannot process photocom (rf)')

    @staticmethod
    def get_attached_image_and_url(media_node: dict) -> tuple[str, str]:
        cur = Jq.first(media_node, 'currMedia')
        if isinstance(cur, dict):
            return str(cur['image']['uri']), str(cur['attached_comment']['feedback']['url'])
        raise ParseException('Cannot process photocom (iau)')

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _route_proven_ids = JsonParser.get_target_ids(
                html_parser, post_path
            )
            content_node = PhotocomParser.get_content_node(html_parser, requested_ids)
            media_node = PhotocomParser.get_media_node(html_parser, requested_ids)
            body = content_node['data']['attached_comment']['preferred_body']

            op_name = content_node['data']['owner']['name'] + ' (💬)'
            post_text = '' if body is None else body['text']
            post_time = content_node['data']['created_time']
            post_image, post_url = PhotocomParser.get_attached_image_and_url(media_node)
            reaction_count = PhotocomParser.get_reaction_count(media_node)
            top_reaction_ids = Utils.get_top_reaction_ids(
                PhotocomParser.get_reaction_feedback(media_node)
            )

            return ParsedPost(op_name, post_text, [post_image], post_url, post_time,
                              Utils.human_format(reaction_count), 'null', 'null', [],
                              top_reaction_ids=top_reaction_ids)


class ReelsParser:
    @staticmethod
    def get_video_link(
        html_parser: BeautifulSoup | None,
        user_node: dict = None,
        requested_ids: list[str] | None = None,
    ) -> str:
        def work_node(node: dict) -> str:
            video_node = node
            if not (Jq.first(video_node, 'browser_native_hd_url') or Jq.first(video_node, 'browser_native_sd_url')):
                video_node = Jq.first(node, 'videoDeliveryLegacyFields')
            for key in ['browser_native_hd_url', 'browser_native_sd_url']:
                try:
                    video_link = Jq.first(video_node, key)
                    if not video_link:
                        continue
                    return str(video_link)
                except StopIteration:
                    pass
            raise ParseException('Invalid reels link (vn)')

        if user_node:
            return work_node(user_node)

        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            candidates.extend(
                node for node in [bloc, *Jq.enumerate(bloc)]
                if Jq.first(node, 'browser_native_hd_url') or Jq.first(node, 'browser_native_sd_url')
            )

        requested_ids = requested_ids or []
        matching = [
            node for node in candidates
            if JsonParser.contains_exact_id(node, requested_ids)
        ]
        if not matching:
            matching = [
                node for node in candidates
                if JsonParser.contains_target_id(node, requested_ids)
            ]
        for node in sorted(matching, key=lambda item: len(str(item))):
            try:
                return work_node(node)
            except (ParseException, KeyError, TypeError, IndexError):
                continue
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        for node in candidates:
            try:
                return work_node(node)
            except (ParseException, KeyError, TypeError, IndexError):
                continue

        raise ParseException('Invalid reels link (vn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup, requested_ids: list[str] | None = None) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            candidates.extend(Jq.all(bloc, 'creation_story'))
            candidates.extend(Jq.all(bloc, 'short_form_video_context'))
        candidates = [node for node in candidates if isinstance(node, dict)]

        requested_ids = requested_ids or []
        for node in candidates:
            if JsonParser.contains_exact_id(node, requested_ids):
                return node
        for node in candidates:
            if JsonParser.contains_target_id(node, requested_ids):
                return node
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        if candidates:
            return candidates[0]
        raise ParseException('Invalid reels link (cn)')

    @staticmethod
    def get_reaction_counts(
        html_parser: BeautifulSoup,
        is_ig: bool,
        video_id: str,
        related_ids: list[str] | None = None,
    ) -> tuple[str, str, str]:
        target_ids = list(dict.fromkeys(
            str(value) for value in [video_id, *(related_ids or [])] if value
        ))
        direct_blocks: list[dict] = []
        url_blocks: list[dict] = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if not Jq.has(bloc, 'unified_reactors'):
                continue
            if JsonParser.contains_exact_id(bloc, target_ids):
                direct_blocks.append(bloc)
            elif JsonParser.contains_target_id(bloc, target_ids):
                url_blocks.append(bloc)

        blocks = direct_blocks or url_blocks

        if len(blocks) == 0:
            raise ParseException('Cannot process post (cn)')

        contextual_feedbacks: list[tuple[set[str], dict]] = []
        id_keys = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
            'storyfbid', 'fbid', 'legacy_fbid', 'top_level_post_id',
        }

        def walk(value, inherited_ids: set[str] | None = None):
            inherited_ids = inherited_ids or set()
            if isinstance(value, dict):
                own_ids = set()
                for key, item in value.items():
                    if str(key).lower() not in id_keys:
                        continue
                    if isinstance(item, (str, int)):
                        own_ids.add(str(item))
                    elif isinstance(item, list):
                        own_ids.update(str(part) for part in item if isinstance(part, (str, int)))
                context_ids = own_ids or inherited_ids
                feedback = value.get('feedback')
                if isinstance(feedback, dict):
                    contextual_feedbacks.append((set(context_ids), feedback))
                elif isinstance(feedback, list):
                    contextual_feedbacks.extend(
                        (set(context_ids), item) for item in feedback if isinstance(item, dict)
                    )
                for item in value.values():
                    walk(item, context_ids)
            elif isinstance(value, list):
                for item in value:
                    walk(item, inherited_ids)

        for block in blocks:
            walk(block)

        target_id_set = set(target_ids)
        feedbacks: list[tuple[set[str], dict]] = []
        seen = set()
        for context_ids, feedback in contextual_feedbacks:
            if not context_ids.intersection(target_id_set) and not JsonParser.contains_target_id(feedback, target_ids):
                continue
            marker = id(feedback)
            if marker not in seen:
                seen.add(marker)
                feedbacks.append((context_ids, feedback))

        if feedbacks:
            def unified_count(item: tuple[set[str], dict]) -> int:
                count = item[1].get('unified_reactors', {}).get('count', 0)
                try:
                    return int(count)
                except (TypeError, ValueError):
                    return 0

            first_context, first_fb = max(feedbacks, key=unified_count)
            context_companions = [
                feedback for context_ids, feedback in feedbacks
                if feedback is not first_fb
                and (not first_context or context_ids.intersection(first_context))
            ]
            first_feedback_id = first_fb.get('id')
            companions = [
                feedback for feedback in context_companions
                if first_feedback_id and feedback.get('id') == first_feedback_id
            ] or context_companions
            last_fb = next((
                fb for fb in reversed(companions)
                if 'total_comment_count' in fb
                or 'share_count_reduced' in fb
                or 'share_count' in fb
            ), None)
            if last_fb is None:
                last_fb = next((
                    fb for fb in reversed(companions)
                    if 'cross_universe_feedback_info' in fb
                ), first_fb)
        else:
            raise ParseException('Cannot associate reactions with requested video')

        cross_info = last_fb.get('cross_universe_feedback_info', {})
        ig_cmts = cross_info.get('ig_comment_count') or last_fb.get('total_comment_count', 0)
        likes = first_fb.get('unified_reactors', {}).get('count', 0)
        cmts = ig_cmts if is_ig else last_fb.get('total_comment_count', 0)
        shares = last_fb.get('share_count_reduced', last_fb.get('share_count', 0))

        return Utils.human_format(likes), Utils.human_format(cmts), Utils.human_format(shares)


    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path, use_cookies=True)
        else:
            page = JsonParser.fetch_page(post_path, use_cookies=True, http_response=http_response)
        with page as html_parser:
            requested_ids, _strong_ids = JsonParser.get_target_ids(html_parser, post_path)
            content_node = ReelsParser.get_content_node(html_parser, requested_ids)

            video_link = ReelsParser.get_video_link(html_parser, requested_ids=requested_ids)
            video_id = next(
                (
                    requested_id for requested_id in requested_ids
                    if JsonParser.contains_target_id(content_node, [requested_id])
                ),
                None,
            )
            video_id = video_id or content_node.get('video', {}).get('id') or content_node.get('id')
            if not video_id:
                video_id = Jq.first(content_node, 'id')

            owner_info = content_node.get('short_form_video_context', {}).get('video_owner') or content_node.get('video_owner')
            if not owner_info:
                owner_info = Jq.first(content_node, 'video_owner')

            is_ig = owner_info['__typename'].startswith('InstagramUser')
            op_name = ('📷 @' if is_ig else '') + owner_info['username' if is_ig else 'name']
            post_url = content_node.get('short_form_video_context', {}).get('shareable_url') or JsonParser.ensure_full_url(post_path)

            post_date = content_node.get('creation_time')
            if not post_date:
                blocks = JsonParser.get_json_blocks(html_parser)
                post_date = (
                    JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
                    or JsonParser.select_requested_field(blocks, 'created_time', requested_ids)
                )
                try:
                    post_date = int(post_date) if post_date else None
                except (ValueError, TypeError):
                    post_date = None
            if not post_date:
                post_date = -1

            post_text = '' if content_node.get('message') is None else content_node['message']['text']

            reaction_ids = [video_id, content_node.get('id'), content_node.get('post_id')]
            content_feedback = content_node.get('feedback')
            if isinstance(content_feedback, dict):
                reaction_ids.append(content_feedback.get('id'))
            likes, cmts, shares = ReelsParser.get_reaction_counts(
                html_parser, is_ig, video_id, reaction_ids
            )

            if owner_info['id'] in config['banned_users']:
                return banned(post_url)

            return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


class VideoWatchParser:
    # excluding group post video since they are handled by jsonparser
    @staticmethod
    def get_op_name(
        html_parser: BeautifulSoup,
        content_node: dict | None = None,
        requested_ids: list[str] | None = None,
    ) -> str:
        if content_node:
            owner = content_node.get('owner') or Jq.first(content_node, 'owner')
            if isinstance(owner, dict) and owner.get('name'):
                return owner['name']
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = requested_ids or []
        owner = JsonParser.select_requested_field(blocks, 'owner', requested_ids)
        if isinstance(owner, dict) and owner.get('name'):
            return owner['name']
        raise ParseException('Invalid watch link (opn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup, requested_ids: list[str] | None = None) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc,'comment_rendering_instance', 'video_view_count_renderer'):
                for result in Jq.all(bloc, 'result'):
                    if isinstance(result, dict) and isinstance(result.get('data'), dict):
                        candidates.append(result['data'])

        requested_ids = requested_ids or []
        for node in candidates:
            if JsonParser.contains_exact_id(node, requested_ids):
                return node
        for node in candidates:
            if JsonParser.contains_target_id(node, requested_ids):
                return node
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        if candidates:
            return candidates[0]
        canonical = html_parser.find('link', attrs={'rel': 'canonical'})
        if canonical:
            canonical_path = urlparse(canonical.get('href', '')).path.rstrip('/')
            if canonical_path == '/watch':
                raise NoDataException('Facebook served generic watch feed instead of specific video')
        raise ParseException('Invalid watch link (cn)')

    @staticmethod
    def get_date(
        html_parser: BeautifulSoup,
        content_node: dict | None = None,
        requested_ids: list[str] | None = None,
    ) -> int:
        if content_node:
            creation_time = content_node.get('creation_time') or Jq.first(content_node, 'creation_time')
            if creation_time:
                return int(creation_time)
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = requested_ids or []
        creation_time = JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
        if creation_time:
            return int(creation_time)
        raise ParseException('cannot find date')

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path, use_cookies=True)
        else:
            page = JsonParser.fetch_page(post_path, use_cookies=True, http_response=http_response)
        with page as html_parser:
            requested_ids, _strong_ids = JsonParser.get_target_ids(html_parser, post_path)
            content_node = VideoWatchParser.get_content_node(html_parser, requested_ids)

            video_link = ReelsParser.get_video_link(html_parser, requested_ids=requested_ids)

            post_url = JsonParser.ensure_full_url(post_path)
            op_name = VideoWatchParser.get_op_name(html_parser, content_node, requested_ids)
            post_text = content_node['title']['text'] if (content_node.get('title') and isinstance(content_node['title'], dict) and content_node['title'].get('text')) else ''
            if not post_text:
                msg = Jq.first(content_node, 'message')
                if isinstance(msg, dict):
                    post_text = msg.get('text', '')

            post_feedback = content_node['feedback']
            likes = Utils.human_format(post_feedback['reaction_count']['count'])
            shares = 'null'
            cmts = Utils.human_format(post_feedback['total_comment_count'])
            post_date = VideoWatchParser.get_date(html_parser, content_node, requested_ids)
            top_reaction_ids = Utils.get_top_reaction_ids(post_feedback)

            return ParsedPost(op_name, post_text, [], post_url, post_date,
                              likes, cmts, shares, [video_link],
                              top_reaction_ids=top_reaction_ids)


def format_error_message_embed(original_url: str) -> str:
    return Utils.prettify(f'''<!DOCTYPE html>
<html lang="">
<head>
<meta charset="UTF-8" />
    <meta name="theme-color" content="#2c3048f" />
    <meta property="og:title" content="Log in or sign up to view"/>
    <meta property="og:description" content="See posts, photos and more on Facebook."/>
    <meta http-equiv="refresh" content="0;url={escape(quote(original_url), quote=True)}"/>
</head>
</html>''')


def is_facebook_url(url: str) -> bool:
    if urlparse(url).netloc:
        url = Utils.normalize_facebook_path(url)
    wwwfb = f'{WWWFB}/'
    username_pattern = '[a-zA-Z0-9-._]*'  # also covers /watch
    full_url = f'{wwwfb}{url}'
    parsed_url = urlparse(full_url)

    is_group_post = re.match(f'^/groups/{username_pattern}', parsed_url.path)
    is_permalink = parsed_url.path.startswith('/permalink.php')
    is_story = parsed_url.path.startswith('/story.php')
    is_post = re.match(f'/{username_pattern}/posts', parsed_url.path)
    is_photo = parsed_url.path.startswith('/photo')
    is_photo_album = re.match(f'/{username_pattern}/photos(?:/|$)', parsed_url.path)

    return is_permalink or is_post or is_story or is_photo or is_photo_album or is_group_post


def format_reel_post_embed(post: ParsedPost) -> str:
    def get_video_meta_tag(link: str) -> str:
        escaped_link = escape(link, quote=True)
        return '\n'.join([
            f'<meta property="twitter:player:stream" content="{escaped_link}"/>',
            f'<meta property="og:video" content="{escaped_link}"/>',
            f'<meta property="og:video:secure_url" content="{escaped_link}"/>',
        ])

    video_meta_tags = '\n'.join([get_video_meta_tag(vu) for vu in post.video_links])
    reaction_str = Utils.format_reactions_str(
        post.likes, post.comments, post.shares, post.top_reaction_ids
    )
    post_date = Utils.timestamp_to_str(post.date)
    site_name = escape(f'{get_credit()}\n{post_date}\n{reaction_str}', quote=True)
    color = '#0866ff'

    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{site_name}"/>
            <meta property="og:url" content="{escape(quote(post.url), quote=True)}"/>
            <meta property="og:video:type" content="video/mp4"/>
            <meta property="twitter:player:stream:content_type" content="video/mp4"/>

            {video_meta_tags}

            <link rel="canonical" href="{escape(quote(post.url), quote=True)}"/>
            <meta http-equiv="refresh" content="0;url={escape(quote(post.url), quote=True)}"/>
            <meta name="twitter:card" content="player"/>
            <meta name="theme-color" content="{color}"/>
        </head>
        </html>''')


def format_full_post_embed(post: ParsedPost) -> str:
    if post.video_links:
        return format_reel_post_embed(post)
    image_links = post.image_links
    image_counter = f'\ncontains 4+ images' if len(image_links) > 4 else ''
    image_links = image_links[:4]
    image_meta_tags = '\n'.join([
        f'<meta property="og:image" content="{escape(iu, quote=True)}"/>'
        for iu in image_links
    ])
    post_date = Utils.timestamp_to_str(post.date)
    reaction_str = Utils.format_reactions_str(
        post.likes, post.comments, post.shares, post.top_reaction_ids
    )
    site_name = escape(
        f'{get_credit()}\n{post_date}\n{reaction_str}{image_counter}', quote=True
    )

    # TODO: organize and duplicate the neccessary tags
    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{site_name}"/>
            <meta property="og:url" content="{escape(quote(post.url), quote=True)}"/>
            {image_meta_tags}
            <link rel="canonical" href="{escape(quote(post.url), quote=True)}"/>
            <meta http-equiv="refresh" content="0;url={escape(quote(post.url), quote=True)}"/>
            <meta name="twitter:card" content="summary_large_image"/>
            <meta name="theme-color" content="#0866ff"/>
        </head>
        </html>''')


def format_redirect_page(url: str) -> str:
    script_url = json.dumps(url).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return Utils.prettify(f'''<!DOCTYPE HTML>
<html lang="en-US">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="refresh" content="0; url={escape(quote(url), quote=True)}">
        <script type="text/javascript">
            window.location.href = {script_url}
        </script>
        <title>redirecting...</title>
    </head>
    <body>
    </body>
</html>''')


def _invoke_parser(parser, post_path: str, http_response: CffiResponse | None = None):
    if http_response is None:
        return parser(post_path)
    return parser(post_path, http_response=http_response)


PARSER_FALLBACK_EXCEPTIONS = (
    NoDataException,
    ParseException,
    KeyError,
    TypeError,
    IndexError,
    ValueError,
    AttributeError,
)
ROUTE_FALLBACK_EXCEPTIONS = PARSER_FALLBACK_EXCEPTIONS + (UpstreamException,)


def _allow_route_upstream_fallback(error: Exception) -> bool:
    return not isinstance(error, UpstreamException) or error.status_code in {
        403, 404, 500, 502, 503, 504,
    }


def _as_parser_error(error: Exception) -> FacebedException:
    if isinstance(error, FacebedException):
        return error
    return ParseException(f'{type(error).__name__}: {error}')


def _prefer_parser_error(first: Exception, second: Exception) -> FacebedException:
    first = _as_parser_error(first)
    second = _as_parser_error(second)
    if isinstance(second, ParseException) and second.html:
        return second
    if isinstance(first, ParseException) and first.html:
        return first
    return second


def _parse_with_generic(parser, post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    try:
        return _invoke_parser(parser, post_path, http_response)
    except PARSER_FALLBACK_EXCEPTIONS as first_error:
        try:
            return _invoke_parser(JsonParser.process_post, post_path, http_response)
        except PARSER_FALLBACK_EXCEPTIONS as second_error:
            raise _prefer_parser_error(first_error, second_error)


def _parse_video_path(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    parsed = urlparse(post_path)
    search = re.search(
        r'(?:^|/)(?:videos/(?:pcb\.\d+/)?|v/)(\d+)', parsed.path, re.IGNORECASE
    )
    if not search:
        search = re.search(r'(?:^|/)[^/]+/v/(\d+)', parsed.path, re.IGNORECASE)
    if not search:
        return _invoke_parser(JsonParser.process_post, post_path, http_response)

    video_id = search.group(1)
    reel_path = f'reel/{video_id}'
    if parsed.query:
        reel_path += f'?{parsed.query}'
    original_response = http_response
    reel_response = http_response
    if http_response is not None:
        final_path = Utils.normalize_facebook_path(str(getattr(http_response, 'url', '')))
        if urlparse(final_path).path.rstrip('/') != urlparse(reel_path).path.rstrip('/'):
            reel_response = None
    try:
        return _invoke_parser(ReelsParser.process_post, reel_path, reel_response)
    except ROUTE_FALLBACK_EXCEPTIONS as reel_error:
        if not _allow_route_upstream_fallback(reel_error):
            raise
        generic_post = None
        generic_error = None
        try:
            generic_post = _invoke_parser(
                JsonParser.process_post, post_path, original_response
            )
            if generic_post.video_links:
                return generic_post
        except ROUTE_FALLBACK_EXCEPTIONS as error:
            generic_error = error
            if not _allow_route_upstream_fallback(error):
                raise
        try:
            return _invoke_parser(
                VideoWatchParser.process_post, post_path, original_response
            )
        except ROUTE_FALLBACK_EXCEPTIONS as watch_error:
            if generic_post is not None:
                return generic_post
            raise _prefer_parser_error(
                _prefer_parser_error(reel_error, generic_error), watch_error
            )


def _ensure_facebook_page_target(post_path: str) -> None:
    parsed_url = urlparse(post_path)
    if parsed_url.netloc:
        hostname = (parsed_url.hostname or '').lower()
        if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
            raise UnsupportedRouteException('refusing to fetch non-Facebook host')


def _dispatch_post(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    _ensure_facebook_page_target(post_path)
    parsed_path = urlparse(post_path).path
    if Utils.is_share_path(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    if (
        re.search(r'(?:^|/)videos/', parsed_path, re.IGNORECASE)
        or re.search(r'(?:^|/)(?:[^/]+/)?v/\d+(?:/|$)', parsed_path, re.IGNORECASE)
    ):
        _mark_parser_pipeline_entry()
        return _parse_video_path(post_path, http_response)
    if re.match(r'^/?reel/[^/?]+', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(ReelsParser.process_post, post_path, http_response)
    if re.match(r'^/?photo(?:\.php)?/?$', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(SinglePhotoParser.process_post, post_path, http_response)
    if re.match(r'^/?watch(?:/|$)', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(VideoWatchParser.process_post, post_path, http_response)
    if is_facebook_url(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    raise UnsupportedRouteException('unsupported Facebook route')


def _successful_embed(parsed_post: ParsedPost) -> str:
    response.status = 200
    response.headers['Cache-Control'] = 'public, max-age=900'
    return format_full_post_embed(parsed_post)


def _send_dump_report(
    path: str,
    http_response: CffiResponse | None = None,
) -> None:
    http_response = http_response or cffi.selected_get_response or cffi.last_get_response
    if http_response is None:
        logging.info('no body-bearing GET captured for dump /%s', path)
        return
    try:
        raw_bytes = bytes(http_response.content or b'')
        filename = re.sub(r'[^a-zA-Z0-9]', '_', path)[:80] + '_dump.html'
        display_path = '/' + path.lstrip('/')
        url = JsonParser.ensure_full_url(path)
        embed = DiscordEmbed(
            title='manual dump report',
            description=f'🔗 [`{display_path}`]({url})\n📋 Manual dump requested by user',
            color='3498DB',
        )
        embed.add_embed_field(name='Attached Payload', value=f'`{filename}`', inline=True)
        embed.add_embed_field(name='Response Size', value=f'{len(raw_bytes)} bytes', inline=True)
        embed.add_embed_field(name='Final URL', value=str(getattr(http_response, 'url', url)), inline=True)
        embed.add_embed_field(name='Status', value=str(getattr(http_response, 'status_code', 'unknown')), inline=True)
        Utils.warn(file_content=raw_bytes, filename=filename, embed=embed)
        logging.info('dump report sent for /%s', path)
    except Exception:
        logging.error("couldn't dump /%s\n%s", path, traceback.format_exc())


def _normalize_account_name(value: str) -> str | None:
    normalized = ' '.join(unescape(value).split()).strip()
    for suffix in (' | Facebook', ' - Facebook'):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)].strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if any(
        marker in lowered
        for marker in (
            'facebook', 'log in', 'login', 'sign up', 'checkpoint',
            'unsupported browser', 'privacy', 'error', 'not found',
        )
    ):
        return None
    return normalized


def _extract_cookie_account_name(
    html_parser: BeautifulSoup,
    body: str = '',
) -> str | None:
    og_title = html_parser.select_one('meta[property="og:title"]')
    if og_title and og_title.get('content'):
        account_name = _normalize_account_name(str(og_title['content']))
        if account_name:
            return account_name
    title = html_parser.select_one('title')
    if title:
        account_name = _normalize_account_name(title.get_text())
        if account_name:
            return account_name
    for block in JsonParser.get_json_blocks(html_parser):
        for candidate in Jq.enumerate(block):
            if not isinstance(candidate, dict) or not (
                'ACCOUNT_ID' in candidate or 'USER_ID' in candidate
            ):
                continue
            for key in ('NAME', 'SHORT_NAME', 'name'):
                value = candidate.get(key)
                if isinstance(value, str):
                    account_name = _normalize_account_name(value)
                    if account_name:
                        return account_name
    marker = 'CurrentUserInitialData'
    offset = 0
    while (start := body.find(marker, offset)) >= 0:
        window = body[start:start + 6000]
        match = re.search(r'"NAME"\s*:\s*"((?:\\.|[^"\\])*)"', window)
        if match:
            try:
                decoded = json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, str):
                account_name = _normalize_account_name(decoded)
                if account_name:
                    return account_name
        offset = start + len(marker)
    return None


def _cookie_probe_blocked_reason(
    final_url: str,
    body: str,
    html_parser: BeautifulSoup,
) -> str | None:
    lowered_url = final_url.lower()
    if '/login' in lowered_url:
        return 'login redirect'
    if '/checkpoint' in lowered_url:
        return 'checkpoint redirect'
    if '/recover' in lowered_url:
        return 'account recovery redirect'
    if 'login_data' in body or 'useCometLogInFormQuery' in body:
        return 'login wall'
    title = html_parser.select_one('title')
    if title:
        lowered_title = title.get_text().lower()
        if 'log in' in lowered_title or 'checkpoint' in lowered_title:
            return 'login title'
    return None


def check_cookie_account(jar: CookieJar, account_index: int) -> CookieAccountCheck:
    account = jar.account_at(account_index)
    if account is None:
        return CookieAccountCheck(
            account_index, f'#{account_index}', False, None, None, 'account missing'
        )
    try:
        with cffi.request_scope(account):
            http_response = cffi.get(
                f'{WWWFB}/me',
                _check_status=False,
                _retry_status_responses=False,
                _bypass_cache=True,
            )
    except UpstreamException as exc:
        return CookieAccountCheck(
            account_index,
            account.label,
            False,
            None,
            exc.status_code,
            f'http: {exc}',
        )

    status = int(http_response.status_code)
    try:
        body = http_response.text
    except Exception as exc:
        return CookieAccountCheck(
            account_index,
            account.label,
            False,
            None,
            status,
            f'read body: {exc}',
        )
    if status < 200 or status >= 300:
        return CookieAccountCheck(
            account_index, account.label, False, None, status, f'status {status}'
        )
    html_parser = BeautifulSoup(body, 'html.parser')
    blocked_reason = _cookie_probe_blocked_reason(
        str(http_response.url), body, html_parser
    )
    if blocked_reason:
        return CookieAccountCheck(
            account_index, account.label, False, None, status, blocked_reason
        )
    account_name = _extract_cookie_account_name(html_parser, body)
    return CookieAccountCheck(
        account_index,
        account.label,
        account_name is not None,
        account_name,
        status,
        None if account_name is not None else 'account name not found',
    )


def check_cookie_accounts(jar: CookieJar) -> list[CookieAccountCheck]:
    return [check_cookie_account(jar, index) for index in range(jar.len())]


def start_cookie_health_check(jar: CookieJar) -> threading.Thread | None:
    if jar.is_empty():
        return None

    def worker() -> None:
        bad_accounts = []
        for check in check_cookie_accounts(jar):
            if check.ok:
                logging.info(
                    "cookie account alive index=%s account='%s' name='%s' status=%s",
                    check.index, check.label, check.account_name or '?', check.status,
                )
            else:
                reason = check.reason or 'unknown'
                logging.warning(
                    "cookie account bad index=%s account='%s' status=%s reason=%s",
                    check.index, check.label, check.status, reason,
                )
                bad_accounts.append(f'{check.label} ({reason})')
        if bad_accounts:
            Utils.warn(
                '@everyone cookie account check failed: ' + ', '.join(bad_accounts)
            )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def _cookie_scope_key(path: str) -> str | None:
    parsed_path = urlparse(JsonParser.ensure_full_url(path)).path.strip('/')
    if parsed_path.startswith('reel/'):
        return 'kind/reels'
    if parsed_path.startswith('watch'):
        return 'kind/watch'
    if parsed_path.startswith('groups/'):
        parts = parsed_path.split('/')
        if len(parts) > 1 and parts[1]:
            return f'groups/{parts[1]}'
    parts = parsed_path.split('/')
    if (
        len(parts) > 1
        and parts[0]
        and parts[1] in {'posts', 'videos', 'photos', 'timeline', 'reels', 'media'}
    ):
        return f'user/{parts[0]}'
    return None


_parser_pipeline_local = threading.local()


@contextmanager
def _parser_pipeline_scope():
    previous = getattr(_parser_pipeline_local, 'state', None)
    state = {'entered': False}
    _parser_pipeline_local.state = state
    try:
        yield state
    finally:
        if previous is None:
            delattr(_parser_pipeline_local, 'state')
        else:
            _parser_pipeline_local.state = previous


def _mark_parser_pipeline_entry() -> None:
    state = getattr(_parser_pipeline_local, 'state', None)
    if state is not None and not state['entered']:
        state['entered'] = True
        service_metrics.record_request()


def _run_tracked_cookie_attempts(
    path: str,
    jar: CookieJar,
) -> tuple[str, CffiResponse | None, bool]:
    with _parser_pipeline_scope() as state:
        try:
            result, selected_response = _run_cookie_attempts(path, jar)
        except Exception as exc:
            setattr(exc, 'parser_pipeline_entered', state['entered'])
            raise
        return result, selected_response, state['entered']


def _record_scrape_error(error: Exception) -> None:
    parser_entered = bool(getattr(error, 'parser_pipeline_entered', False))
    if not parser_entered:
        if isinstance(error, UnsupportedRouteException):
            return
        if isinstance(error, ShareResolutionException):
            if not error.account_backed and error.count_as_error:
                service_metrics.record_error()
            return
    if not parser_entered:
        service_metrics.record_request()
    service_metrics.record_error()


def _record_cookie_failure(
    jar: CookieJar,
    account_index: int,
    error: Exception,
    affinity_key: str | None,
) -> int:
    upstream_response = getattr(error, 'upstream_response', None)
    status = getattr(upstream_response, 'status_code', None)
    final_url = str(getattr(upstream_response, 'url', '')).lower()
    if status in (429, 503):
        raw_retry_after = getattr(upstream_response, 'headers', {}).get('Retry-After')
        try:
            retry_after = int(str(raw_retry_after).strip())
        except (TypeError, ValueError):
            retry_after = None
        if retry_after is not None and retry_after < 0:
            retry_after = None
        jar.mark_rate_limited(account_index, retry_after)
        return 0
    if '/checkpoint' in final_url or '/recover' in final_url:
        count = jar.mark_checkpointed(account_index)
    else:
        count = jar.mark_failed(account_index)
    if affinity_key and jar.affinity_for(affinity_key) == account_index:
        jar.forget_affinity(affinity_key)
    return count


def _maybe_notify_bad_account(
    jar: CookieJar,
    account_index: int,
    count: int,
    error: Exception,
) -> None:
    if count != NOTIFY_FAILURE_THRESHOLD:
        return
    account = jar.account_at(account_index)
    label = account.label if account is not None else '?'
    Utils.warn(
        f'@everyone account `{label}` failed {count}× in a row — cookie likely expired or checkpointed. '
        f'Please re-export and update `cookies-{label}.json`. Last error: `{error}`'
    )
    jar.reset_failure_count(account_index)


def _is_cookie_retryable(error: Exception) -> bool:
    if isinstance(error, (NoDataException, ParseException)):
        return True
    if not isinstance(error, UpstreamException):
        return False
    upstream_response = error.upstream_response
    if upstream_response is None:
        return True
    status = getattr(upstream_response, 'status_code', None)
    final_url = str(getattr(upstream_response, 'url', '')).lower()
    return (
        status in CFFI.retry_statuses
        or '/checkpoint' in final_url
        or '/recover' in final_url
    )


def _run_cookie_attempts(
    path: str,
    jar: CookieJar,
) -> tuple[str, CffiResponse | None]:
    _ensure_facebook_page_target(path)
    if jar.is_empty():
        with cffi.request_scope():
            try:
                result = _handle_facebook_path(path)
            except Exception as exc:
                selected_response = (
                    cffi.selected_get_response or cffi.last_get_response
                )
                _attach_attempt_response(exc, selected_response)
                raise
            return result, cffi.selected_get_response or cffi.last_get_response

    if Utils.is_share_path(path) and '3' not in request.query.getall('type'):
        return _run_share_cookie_attempts(path, jar)

    affinity_key = _cookie_scope_key(path)
    order = jar.account_order(affinity_key)
    for position, account_index in enumerate(order):
        account = jar.account_at(account_index)
        with cffi.request_scope(account):
            try:
                result = _handle_facebook_path(path)
            except (NoDataException, ParseException, UpstreamException) as exc:
                if isinstance(exc, UnsupportedRouteException):
                    raise
                selected_response = cffi.selected_get_response or cffi.last_get_response
                _attach_attempt_response(exc, selected_response)
                if isinstance(exc, ShareResolutionException):
                    exc.account_backed = True
                    if position + 1 < len(order):
                        continue
                    raise
                failure_affinity_key = _cookie_scope_key(cffi.affinity_path or path)
                failure_count = _record_cookie_failure(
                    jar, account_index, exc, failure_affinity_key
                )
                _maybe_notify_bad_account(jar, account_index, failure_count, exc)
                if _is_cookie_retryable(exc) and position + 1 < len(order):
                    continue
                raise
            except Exception as exc:
                _attach_attempt_response(
                    exc, cffi.selected_get_response or cffi.last_get_response
                )
                raise
            selected_response = cffi.selected_get_response or cffi.last_get_response
            success_affinity_key = _cookie_scope_key(cffi.affinity_path or path)
        jar.mark_ok(account_index)
        if success_affinity_key:
            jar.set_affinity(success_affinity_key, account_index)
        return result, selected_response

    raise NoDataException('no accounts available')


def _attach_attempt_response(
    error: Exception,
    selected_response: CffiResponse | None,
) -> None:
    if (
        getattr(error, 'upstream_response', None) is None
        and selected_response is not None
    ):
        setattr(error, 'upstream_response', selected_response)
    setattr(error, 'selected_response', selected_response)


def _run_share_cookie_attempts(
    source_path: str,
    jar: CookieJar,
) -> tuple[str, CffiResponse | None]:
    resolved_path = None
    prefetched_response = None
    resolution_selected_response = None
    resolver_account_index = None
    last_resolution_error = None

    for account_index in jar.account_order(None):
        account = jar.account_at(account_index)
        with cffi.request_scope(account):
            try:
                candidate_path, candidate_response = Utils.resolve_share_link(
                    source_path
                )
            except ShareResolutionException as exc:
                selected_response = (
                    cffi.selected_get_response or cffi.last_get_response
                )
                _attach_attempt_response(exc, selected_response)
                exc.account_backed = True
                last_resolution_error = exc
                continue
            except Exception as exc:
                _attach_attempt_response(
                    exc, cffi.selected_get_response or cffi.last_get_response
                )
                raise
            selected_response = cffi.selected_get_response or cffi.last_get_response

        resolved_path = candidate_path
        prefetched_response = candidate_response
        resolution_selected_response = selected_response or candidate_response
        resolver_account_index = account_index
        break

    if resolved_path is None:
        if last_resolution_error is not None:
            raise last_resolution_error
        error = ShareResolutionException('no accounts available for share resolution')
        error.account_backed = True
        raise error

    affinity_key = _cookie_scope_key(resolved_path)
    order = jar.account_order(affinity_key)
    for position, account_index in enumerate(order):
        account = jar.account_at(account_index)
        attempt_prefetched_response = (
            prefetched_response
            if account_index == resolver_account_index
            else None
        )
        with cffi.request_scope(account):
            cffi.set_affinity_path(resolved_path)
            try:
                result = _handle_resolved_facebook_path(
                    resolved_path,
                    prefetched_response=attempt_prefetched_response,
                    share_source_path=source_path,
                )
            except (NoDataException, ParseException, UpstreamException) as exc:
                if isinstance(exc, UnsupportedRouteException):
                    raise
                selected_response = (
                    cffi.selected_get_response
                    or cffi.last_get_response
                    or attempt_prefetched_response
                    or (
                        resolution_selected_response
                        if account_index == resolver_account_index
                        else None
                    )
                )
                _attach_attempt_response(exc, selected_response)
                failure_count = _record_cookie_failure(
                    jar, account_index, exc, affinity_key
                )
                _maybe_notify_bad_account(
                    jar, account_index, failure_count, exc
                )
                if _is_cookie_retryable(exc) and position + 1 < len(order):
                    continue
                raise
            except Exception as exc:
                _attach_attempt_response(
                    exc,
                    cffi.selected_get_response
                    or cffi.last_get_response
                    or attempt_prefetched_response,
                )
                raise
            selected_response = (
                cffi.selected_get_response
                or cffi.last_get_response
                or attempt_prefetched_response
                or (
                    resolution_selected_response
                    if account_index == resolver_account_index
                    else None
                )
            )

        jar.mark_ok(account_index)
        if affinity_key:
            jar.set_affinity(affinity_key, account_index)
        return result, selected_response

    raise NoDataException('no accounts available')


def _handle_facebook_path(path: str) -> str:
    prefetched_response = None
    if '3' in request.query.getall('type'):
        try:
            _mark_parser_pipeline_entry()
            return _successful_embed(PhotocomParser.process_post(path))
        except PARSER_FALLBACK_EXCEPTIONS:
            pass

    share_source_path = None
    if Utils.is_share_path(path):
        share_source_path = path
        path, prefetched_response = Utils.resolve_share_link(path)
        cffi.set_affinity_path(path)

    return _handle_resolved_facebook_path(
        path,
        prefetched_response=prefetched_response,
        share_source_path=share_source_path,
    )


def _handle_resolved_facebook_path(
    path: str,
    prefetched_response: CffiResponse | None = None,
    share_source_path: str | None = None,
) -> str:

    try:
        parsed_post = _dispatch_post(path, prefetched_response)
    except ROUTE_FALLBACK_EXCEPTIONS as primary_error:
        if not _allow_route_upstream_fallback(primary_error):
            raise
        if share_source_path and share_source_path != path:
            try:
                _mark_parser_pipeline_entry()
                parsed_post = _invoke_parser(
                    JsonParser.process_post,
                    share_source_path,
                    prefetched_response,
                )
            except ROUTE_FALLBACK_EXCEPTIONS as share_error:
                if not _allow_route_upstream_fallback(share_error):
                    raise
                raise _prefer_parser_error(primary_error, share_error)
        else:
            raise
    return _successful_embed(parsed_post)


@app.route('/healthz')
def healthz():
    jar = cookie_store.snapshot()
    uptime_secs, request_count, error_count = service_metrics.snapshot()
    payload = {
        'status': 'ok',
        'uptime_secs': uptime_secs,
        'requests': request_count,
        'errors': error_count,
        'cookie_accounts': jar.len(),
        'accounts': [
            {
                'label': account.label,
                'in_cooldown': jar.in_cooldown(index),
            }
            for index, account in enumerate(jar.accounts)
        ],
    }
    response.content_type = 'application/json'
    response.headers['Cache-Control'] = 'no-store'
    return json.dumps(payload)


@app.route('/<path:path>')
def index(path: str):
    dump_requested = bool(re.search(r'(?:^|/)dump/?$', path, re.IGNORECASE))
    if dump_requested:
        path = re.sub(r'(?:^|/)dump/?$', '', path, flags=re.IGNORECASE)
    path = path.rstrip('/')
    if request.query_string:
        path = f'{path}?{request.query_string}'
    original_path = path

    response.headers['Vary'] = 'User-Agent'
    response.headers['Cache-Control'] = 'no-store'
    if not crawleruseragents.is_crawler(
        request.headers.get('User-Agent', ''), case_sensitive=False
    ):
        response.status = 302
        response.headers['Location'] = f'{WWWFB}/{path}'
        return format_redirect_page(f'{WWWFB}/{path}')

    selected_response = None
    jar = cookie_store.snapshot()
    try:
        result, selected_response, parser_entered = _run_tracked_cookie_attempts(
            path, jar
        )
        if not parser_entered:
            service_metrics.record_request()
    except UpstreamException as exc:
        _record_scrape_error(exc)
        selected_response = getattr(exc, 'selected_response', None)
        status = exc.status_code
        if status in (403, 404):
            response.status = 404
        elif status in CFFI.retry_statuses or status is None or (status and status >= 500):
            response.status = 503
            response.headers['Retry-After'] = str(exc.retry_after or 60)
        else:
            response.status = 502
        response.headers['Cache-Control'] = 'no-store'
        logging.warning('upstream failure on /%s: %s', original_path, exc)
        result = format_error_message_embed(f'{WWWFB}/{original_path}')
    except NoDataException as exc:
        _record_scrape_error(exc)
        selected_response = getattr(exc, 'selected_response', None)
        response.status = 404
        response.headers['Cache-Control'] = 'no-store'
        logging.info('no data for /%s (login wall / restricted)', original_path)
        result = format_error_message_embed(f'{WWWFB}/{original_path}')
    except ParseException as exc:
        _record_scrape_error(exc)
        selected_response = getattr(exc, 'selected_response', None)
        response.status = 502
        response.headers['Cache-Control'] = 'no-store'
        logging.error('parser bug on /%s\n%s', original_path, traceback.format_exc())
        page_url = exc.url or f'{WWWFB}/{original_path}'
        filename = re.sub(r'[^a-zA-Z0-9]', '_', original_path)[:80] + '.html' if exc.html else None
        display_path = '/' + original_path.lstrip('/')
        desc = f'🔗 [`{display_path}`]({page_url})\n🚩 {exc}'
        if filename:
            desc += ' and attached file'
        embed = DiscordEmbed(title='embed failure', description=desc, color='FF0000')
        if filename:
            embed.add_embed_field(name='Attached Payload', value=f'`{filename}`', inline=True)
        if exc.html:
            Utils.warn(file_content=exc.html.encode('utf-8'), filename=filename, embed=embed)
        else:
            Utils.warn(embed=embed)
        result = format_error_message_embed(f'{WWWFB}/{original_path}')
    except FacebedException as exc:
        _record_scrape_error(exc)
        selected_response = getattr(exc, 'selected_response', None)
        response.status = 502
        response.headers['Cache-Control'] = 'no-store'
        logging.warning('Facebed failure on /%s\n%s', original_path, traceback.format_exc())
        result = format_error_message_embed(f'{WWWFB}/{original_path}')
    except Exception as exc:
        _record_scrape_error(exc)
        response.status = 502
        response.headers['Cache-Control'] = 'no-store'
        logging.error('something broke on /%s\n%s', original_path, traceback.format_exc())
        result = format_error_message_embed(f'{WWWFB}/{original_path}')

    if dump_requested:
        _send_dump_report(path, selected_response)
        response.headers['Cache-Control'] = 'no-store'
    return result


@app.route('/favicon.ico')
def favicon():
    response.content_type = 'image/x-icon'
    return static_file('favicon.ico', root=str(ASSETS_DIR))


@app.route('/banner.png')
def banner():
    response.content_type = 'image/png'
    return static_file('banner.png', root=str(ASSETS_DIR))


@app.route('/')
def root():
    with (ASSETS_DIR / 'index.html').open(encoding='utf-8') as f:
        return f.read().replace('{|CREDIT|}', get_credit())


def log_to_logger(fn):
    @wraps(fn)
    def _log_to_logger(*argsz, **kwargs):
        actual_response = fn(*argsz, **kwargs)
        title = 'unknown'
        if isinstance(actual_response, str):
            error_match = re.search(r'content="Log in or sign up to view \[(.*)\]"', actual_response)
            if error_match:
                title = f'Error: {error_match.group(1)}'
            else:
                title_match = re.search(r'content="([^"]*)"', actual_response)
                if title_match:
                    title = title_match.group(1)
        logging.info('%s %s %s %s %s' % (request.remote_addr, request.method, request.url, response.status, title))
        return actual_response

    return _log_to_logger


def main():
    global config

    parser = argparse.ArgumentParser(description='Facebook embed server')
    parser.add_argument('-c', '--config', type=str, help='config yaml file path')
    parser.add_argument(
        '--cookies',
        type=Path,
        default=Path('cookies.json'),
        help='path to cookies.json (sibling cookies*.json files are auto-discovered)',
    )
    args = parser.parse_args()

    if args.config:
        if not os.path.isfile(args.config):
            logging.error(f'config file {args.config} not found or is not a file')
            exit(1)
        if not os.access(args.config, os.R_OK):
            logging.error(f'config file {args.config} not readable')
            exit(1)

        with open(args.config, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        for dk in default_config:
            if dk not in config:
                config[dk] = default_config[dk]
        for k in config:
            if k not in default_config or type(config[k]) != type(default_config[k]):
                logging.error(f'invalid config entry {k}')
                exit(1)
    else:
        config = default_config

    if config['timezone'] < -12 or config['timezone'] > 14:
        logging.critical('invalid timezone offset')
        exit(1)

    if sys.version_info.minor < 12:
        logging.error('python 3.12+ required, see https://docs.python.org/3.12/whatsnew/3.12.html#pep-701-syntactic-formalization-of-f-strings')
        exit(1)

    loaded_jar = CookieJar.load(args.cookies)
    cookie_store.replace(loaded_jar)
    install_cookie_reload_handler(args.cookies)
    start_cookie_health_check(loaded_jar)

    logging.info(f'listening on {config["host"]}:{config["port"]}')
    app.install(log_to_logger)
    app.run(host=config['host'], port=config['port'], quiet=True)


if __name__ == '__main__':
    main()
