import argparse
import base64
import io
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from functools import wraps
from html import escape
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


class FacebedException(Exception):
    pass


class NoDataException(FacebedException):
    pass


class ParseException(FacebedException):
    def __init__(self, message: str, html: str = '', url: str = ''):
        super().__init__(message)
        self.html = html
        self.url = url


class UpstreamException(FacebedException):
    def __init__(self, message: str, response: CffiResponse | None = None):
        super().__init__(message)
        self.upstream_response = response
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
    def request_scope(self):
        if getattr(self._local, 'session', None) is not None:
            yield self._local.session
            return

        session = CffiSession(
            impersonate=self.impersonate,
            headers=JsonParser.get_headers(),
            discard_cookies=True,
        )
        self._local.session = session
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
                    'session', 'get_cache', 'responses', 'last_get_response',
                    'selected_get_response',
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
        headers = dict(kwargs.pop('headers', {}))
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
            f'Facebook request failed for {url}: {last_error}', last_response
        ) from last_error


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

WWWFB = 'https://www.facebook.com'
TZ_OFFSET: int = 0
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
    def resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        with cffi.request_scope():
            return Utils._resolve_share_link(path)

    @staticmethod
    def _resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        source_path = Utils.normalize_facebook_path(path)
        url = JsonParser.ensure_full_url(source_path)
        logging.info(f'resolving share link {url}')
        head_response = None
        try:
            head_response = cffi.head(url, _check_status=False)
        except UpstreamException as exc:
            logging.warning('share HEAD failed for %s: %s', url, exc)

        needs_get = head_response is None
        if head_response is not None:
            head_path = Utils.normalize_facebook_path(str(head_response.url))
            head_url_path = urlparse(str(head_response.url)).path.lower()
            needs_get = (
                head_response.status_code >= 400
                or Utils.is_share_path(head_path)
                or head_url_path.startswith('/login')
            )
            if not needs_get:
                logging.info(f'resolved to {head_response.url}')
                return head_path, None

        def inspect_direct(response: CffiResponse):
            direct_url = str(response.url)
            direct_path = Utils.normalize_facebook_path(direct_url)
            direct_url_path = urlparse(direct_url).path
            html_parser = BeautifulSoup(response.text, 'html.parser')
            page_type = JsonParser.probe_page_type(html_parser, direct_path)
            if page_type != 'has_data' and source_path != direct_path:
                page_type = JsonParser.probe_page_type(html_parser, source_path)
            return direct_url, direct_path, direct_url_path, page_type

        direct_response = cffi.get(
            url,
            _check_status=False,
            _retry_status_responses=False,
        )
        direct_url, direct_path, direct_url_path, page_type = inspect_direct(direct_response)
        if (
            page_type != 'has_data'
            and direct_response.status_code in cffi.retry_statuses
        ):
            direct_response = cffi.get(
                url,
                _check_status=False,
                _retry_status_responses=False,
                _bypass_cache=True,
            )
            direct_url, direct_path, direct_url_path, page_type = inspect_direct(direct_response)
        logging.info(f'resolved to {direct_url}')
        if direct_response.status_code >= 400 and page_type != 'has_data':
            raise UpstreamException(
                f'Facebook returned HTTP {direct_response.status_code} for {url}', direct_response
            )
        if Utils.is_share_path(direct_path):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException('Facebook left the share URL unresolved without post data')
        if direct_url_path.lower().startswith('/login'):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException('Facebook redirected share link to login')

        parsed_direct = urlparse(direct_url)
        direct_host = (parsed_direct.hostname or '').lower()
        if direct_host and direct_host != 'facebook.com' and not direct_host.endswith('.facebook.com'):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException('Facebook redirected share link outside Facebook')
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
    def d(o, no):
        with open(f'test{no}.json', 'w', encoding='utf-8') as f:
            f.write(json.dumps(o, ensure_ascii=False, indent=2))

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
    def format_reactions_str(likes: str, cmts: str, shares: str) -> str:
        likes_str = f'❤️ {likes}' if likes != 'null' else ''
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

    @staticmethod
    def last(obj: dict, key: str) -> dict:
        return Jq.iterate(obj, key)[-1]


class Cookies:
    def __init__(self, fn: str):
        self.fn = Path(fn)
        self.cookies: list = []
        self._signature = object()
        self._warned_signature = None
        self._lock = threading.Lock()
        self._reload_if_changed()

    def _file_signature(self):
        try:
            stat = self.fn.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _reload_if_changed(self) -> None:
        signature = self._file_signature()
        if signature == self._signature:
            return

        self._signature = signature
        self._warned_signature = None
        if signature is None:
            self.cookies = []
            logging.warning(
                '%s not found or unreadable, non incognito-viewable posts will NOT work',
                self.fn.name,
            )
            return

        try:
            with self.fn.open(encoding='utf-8') as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self.cookies = []
            logging.warning("couldn't load %s: %s", self.fn, exc)
            return

        self.cookies = loaded if isinstance(loaded, list) else []
        logging.info(f'loaded {len(self.cookies)} cookies from {self.fn}')

    def is_valid_cookie(self, entry: dict) -> bool:
        if not isinstance(entry, dict) or 'name' not in entry or 'value' not in entry:
            return False
        expiration = entry.get('expirationDate', 2**31)
        if expiration in (None, ''):
            return True
        try:
            return float(expiration) > time.time()
        except (TypeError, ValueError):
            return False

    def get_cookies(self) -> dict[str, str]:
        with self._lock:
            self._reload_if_changed()
            valid_cookies = [cookie for cookie in self.cookies if self.is_valid_cookie(cookie)]
            expired_cookies = [cookie for cookie in self.cookies if not self.is_valid_cookie(cookie)]
            if expired_cookies and self._warned_signature != self._signature:
                self._warned_signature = self._signature
                names = ', '.join(
                    str(cookie.get('name', '?')) if isinstance(cookie, dict) else '?'
                    for cookie in expired_cookies
                )
                Utils.warn(f'@everyone expired cookies ignored: {names}')

            return {
                cookie['name']: cookie['value']
                for cookie in valid_cookies
                if 'name' in cookie and 'value' in cookie
            }


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
        elif story_json.get('message') and isinstance(story_json['message'], dict) and story_json['message'].get('text'):
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
    def contains_requested_id(
        value,
        requested_ids: list[str],
        field_name: str = '',
        allow_url_fields: bool = True,
    ) -> bool:
        if not requested_ids:
            return False
        if isinstance(value, dict):
            return any(
                JsonParser.contains_requested_id(
                    item,
                    requested_ids,
                    str(key).lower(),
                    allow_url_fields,
                )
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(
                JsonParser.contains_requested_id(
                    item,
                    requested_ids,
                    field_name,
                    allow_url_fields,
                )
                for item in value
            )
        if not isinstance(value, (str, int)):
            return False

        id_fields = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid', 'storyfbid', 'fbid', 'legacy_fbid',
            'story_id', 'storyid', 'top_level_post_id', 'mf_story_key', 'feedback_id',
        }
        url_fields = {
            'url', 'wwwurl', 'href', 'permalink', 'shareable_url', 'canonical_url',
        }
        if field_name not in id_fields and (
            not allow_url_fields or field_name not in url_fields
        ):
            return False

        text = str(value)
        if field_name in url_fields:
            if not allow_url_fields:
                return False
            url_ids = JsonParser.get_requested_ids(text)
            return bool(set(requested_ids).intersection(url_ids))

        for requested_id in requested_ids:
            if text == requested_id:
                return True
            if requested_id.isdigit():
                if re.search(
                    rf'(?<![A-Za-z0-9]){re.escape(requested_id)}(?![A-Za-z0-9])',
                    text,
                ):
                    return True
        return False

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
    ) -> tuple[str, str, str]:
        assert post_json

        def extract_counts(fb: dict) -> tuple[str, str, str]:
            reactions = fb.get('i18n_reaction_count') or Jq.first(fb, 'i18n_reaction_count') or '0'
            shares = fb.get('i18n_share_count') or fb.get('share_count') or Jq.first(fb, 'i18n_share_count') or Jq.first(fb, 'share_count') or '0'
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

        requested_ids = requested_ids or []
        if requested_ids:
            contextual_feedbacks: list[dict] = []
            id_keys = {
                'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
                'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
                'top_level_post_id', 'mf_story_key', 'feedback_id',
            }

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
            direct_feedback = post_json.get('feedback')
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
                def reaction_score(feedback: dict) -> int:
                    try:
                        return int(str(feedback.get('i18n_reaction_count', 0)).replace(',', ''))
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

        ufi_in_sections = Jq.first(post_json, 'comet_ufi_summary_and_actions_renderer')
        if ufi_in_sections:
            ufi_feedback = ufi_in_sections.get('feedback')
            if isinstance(ufi_feedback, dict):
                reactions = ufi_feedback.get('i18n_reaction_count') or Jq.first(ufi_feedback, 'i18n_reaction_count') or '0'
                shares = ufi_feedback.get('i18n_share_count') or ufi_feedback.get('share_count') or Jq.first(ufi_feedback, 'i18n_share_count') or Jq.first(ufi_feedback, 'share_count') or '0'
                comments = ufi_feedback.get('total_comment_count') or Jq.first(ufi_feedback, 'total_comment_count')
                if not comments:
                    cri = ufi_feedback.get('comment_rendering_instance')
                    if isinstance(cri, dict):
                        cnode = cri.get('comments')
                        if isinstance(cnode, dict):
                            comments = cnode.get('total_count')
                    if not comments:
                        ccsr = ufi_feedback.get('comments_count_summary_renderer')
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
                if reactions != '0' or shares != '0' or comments != '0':
                    return str(reactions), str(comments), str(shares)

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
            likes, cmts, shares = JsonParser.get_interaction_counts(
                post_json, requested_ids
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
                              likes, cmts, shares, story.video_links)


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
            interaction_node = SinglePhotoParser.get_interactions_node(
                html_parser, requested_ids
            )

            post_text = content_node['message']['text'] if content_node['message'] and 'text' in content_node['message'] else ''
            post_author = content_node['owner']['name']
            post_date = content_node['created_time']
            if interaction_node is None:
                likes, cmts, shares = '0', '0', '0'
            else:
                likes, cmts, shares = JsonParser.get_interaction_counts(
                    interaction_node, requested_ids
                )
            image_url = SinglePhotoParser.get_single_image(html_parser, requested_ids)

            return ParsedPost(post_author, post_text.strip(), [image_url], JsonParser.ensure_full_url(post_path),
                              post_date, likes, cmts, shares, [])


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
        reactors = Jq.first(media_node, 'unified_reactors')
        if isinstance(reactors, dict) and 'count' in reactors:
            return reactors['count']
        raise ParseException('Cannot process photocom (rc)')

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

            return ParsedPost(op_name, post_text, [post_image], post_url, post_time, Utils.human_format(reaction_count), 'null', 'null', [])


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
    def get_reaction_counts(html_parser: BeautifulSoup, is_ig: bool, video_id: str) -> tuple[str, str, str]:
        direct_blocks: list[dict] = []
        url_blocks: list[dict] = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if not Jq.has(bloc, 'unified_reactors'):
                continue
            if JsonParser.contains_exact_id(bloc, [str(video_id)]):
                direct_blocks.append(bloc)
            elif JsonParser.contains_target_id(bloc, [str(video_id)]):
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

        target = str(video_id)
        feedbacks = []
        seen = set()
        for context_ids, feedback in contextual_feedbacks:
            if target not in context_ids and not JsonParser.contains_target_id(feedback, [target]):
                continue
            marker = id(feedback)
            if marker not in seen:
                seen.add(marker)
                feedbacks.append(feedback)

        if feedbacks:
            first_fb = next((fb for fb in feedbacks if 'unified_reactors' in fb), feedbacks[0])
            last_fb = next((
                fb for fb in reversed(feedbacks)
                if 'cross_universe_feedback_info' in fb
                or 'total_comment_count' in fb
                or 'share_count_reduced' in fb
            ), feedbacks[-1])
        else:
            raise ParseException('Cannot associate reactions with requested video')

        if 'cross_universe_feedback_info' in str(first_fb):
            first_fb, last_fb = last_fb, first_fb

        cross_info = last_fb.get('cross_universe_feedback_info', {})
        ig_cmts = cross_info.get('ig_comment_count', last_fb.get('total_comment_count', 0))
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

            likes, cmts, shares = ReelsParser.get_reaction_counts(html_parser, is_ig, video_id)

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

            likes = Utils.human_format(content_node['feedback']['reaction_count']['count'])
            shares = 'null'
            cmts = Utils.human_format(content_node['feedback']['total_comment_count'])
            post_date = VideoWatchParser.get_date(html_parser, content_node, requested_ids)

            return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


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
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)
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
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)
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


def process_post(post_path: str, http_response: CffiResponse | None = None) -> str:
    post_path = Utils.normalize_facebook_path(post_path)
    if http_response is None:
        parsed_post = JsonParser.process_post(post_path)
    else:
        parsed_post = JsonParser.process_post(post_path, http_response=http_response)
    if type(parsed_post) == ParsedPost:
        return format_full_post_embed(parsed_post)
    return format_error_message_embed(f'{WWWFB}/{post_path}')


def process_single_photo(post_path: str, http_response: CffiResponse | None = None) -> str:
    if http_response is None:
        parsed_post = SinglePhotoParser.process_post(post_path)
    else:
        parsed_post = SinglePhotoParser.process_post(post_path, http_response=http_response)
    if type(parsed_post) == ParsedPost:
        return format_full_post_embed(parsed_post)
    return format_error_message_embed(f'{WWWFB}/{post_path}')


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
        try:
            return _invoke_parser(JsonParser.process_post, post_path, original_response)
        except ROUTE_FALLBACK_EXCEPTIONS as generic_error:
            if not _allow_route_upstream_fallback(generic_error):
                raise
            try:
                return _invoke_parser(VideoWatchParser.process_post, post_path, original_response)
            except ROUTE_FALLBACK_EXCEPTIONS as watch_error:
                raise _prefer_parser_error(
                    _prefer_parser_error(reel_error, generic_error), watch_error
                )


def _dispatch_post(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    parsed_path = urlparse(post_path).path
    if Utils.is_share_path(post_path):
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    if (
        re.search(r'(?:^|/)videos/', parsed_path, re.IGNORECASE)
        or re.search(r'(?:^|/)(?:[^/]+/)?v/\d+(?:/|$)', parsed_path, re.IGNORECASE)
    ):
        return _parse_video_path(post_path, http_response)
    if re.match(r'^/?reel/[^/?]+', parsed_path, re.IGNORECASE):
        return _parse_with_generic(ReelsParser.process_post, post_path, http_response)
    if re.match(r'^/?photo(?:\.php)?/?$', parsed_path, re.IGNORECASE):
        return _parse_with_generic(SinglePhotoParser.process_post, post_path, http_response)
    if re.match(r'^/?watch(?:/|$)', parsed_path, re.IGNORECASE):
        return _parse_with_generic(VideoWatchParser.process_post, post_path, http_response)
    if is_facebook_url(post_path):
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    raise NoDataException('unsupported Facebook route')


def _successful_embed(parsed_post: ParsedPost) -> str:
    response.status = 200
    response.headers['Cache-Control'] = 'public, max-age=900'
    return format_full_post_embed(parsed_post)


def _send_dump_report(path: str) -> None:
    http_response = cffi.selected_get_response or cffi.last_get_response
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


def _handle_facebook_path(path: str) -> str:
    prefetched_response = None
    if '3' in request.query.getall('type'):
        try:
            return _successful_embed(PhotocomParser.process_post(path))
        except PARSER_FALLBACK_EXCEPTIONS:
            pass

    share_source_path = None
    if Utils.is_share_path(path):
        share_source_path = path
        path, prefetched_response = Utils.resolve_share_link(path)

    try:
        parsed_post = _dispatch_post(path, prefetched_response)
    except ROUTE_FALLBACK_EXCEPTIONS as primary_error:
        if not _allow_route_upstream_fallback(primary_error):
            raise
        if share_source_path and share_source_path != path:
            try:
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

    with cffi.request_scope():
        try:
            result = _handle_facebook_path(path)
        except UpstreamException as exc:
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
        except NoDataException:
            response.status = 404
            response.headers['Cache-Control'] = 'no-store'
            logging.info('no data for /%s (login wall / restricted)', original_path)
            result = format_error_message_embed(f'{WWWFB}/{original_path}')
        except ParseException as exc:
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
            response.status = 502
            response.headers['Cache-Control'] = 'no-store'
            logging.warning('Facebed failure on /%s\n%s', original_path, traceback.format_exc())
            result = format_error_message_embed(f'{WWWFB}/{original_path}')
        except Exception:
            response.status = 502
            response.headers['Cache-Control'] = 'no-store'
            logging.error('something broke on /%s\n%s', original_path, traceback.format_exc())
            result = format_error_message_embed(f'{WWWFB}/{original_path}')

        if dump_requested:
            _send_dump_report(path)
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

    logging.info(f'listening on {config["host"]}:{config["port"]}')
    app.install(log_to_logger)
    app.run(host=config['host'], port=config['port'], quiet=True)


if __name__ == '__main__':
    main()
