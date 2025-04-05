import argparse
import concurrent.futures
import datetime
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from functools import wraps, lru_cache
from io import BytesIO
from typing import Self, Callable
from urllib.parse import urlparse

import requests as goofy_requests
import stealth_requests as requests
from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFont import FreeTypeFont
from bottle import Bottle, request, template, response, static_file
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook
from pilmoji import Pilmoji
from yattag import indent

import user_config

app: Bottle = Bottle()

WWWFB = 'https://www.facebook.com'
TZ_OFFSET: int = 0
ALLOW_UPDATE = True
logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(msg)s', level=logging.INFO)


def get_credit() -> str:
    cred_mid: str = f'facebed by pi.kt{"🎂" if date.today().month == 12 and date.today().day == 28 else ""}'
    return cred_mid
    # if minify:
    #     return cred_mid
    # cred: str = f'{cred_mid} • embed with s/book/bed'
    # return cred


class Utils:
    @staticmethod
    def resolve_share_link(path: str) -> str:
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'priority': 'u=0, i',
            'referer': 'https://www.google.com/',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
        }
        headers.update(Utils.get_ua())

        # cookies not needed to resolve share links
        head_request = goofy_requests.head(f'{WWWFB}/{path}', headers=headers)
        if head_request.next is None or head_request.next.url.startswith('https://www.facebook.com/share'):
            return ''
        path = head_request.next.url.removeprefix(f'{WWWFB}/')
        return path


    @staticmethod
    def prettify(txt: str) -> str:
        return indent(txt, indentation ='    ', newline = '\n', indent_text = True)

    @staticmethod
    def get_ua() -> dict:
        if user_config.USER_AGENT:
            return {'user-agent': user_config.USER_AGENT}
        else:
            return {}

    @staticmethod
    def warn(msg: str):
        def worker():
            if user_config.BANNED_USER_IDS is None:
                return
            webhook = DiscordWebhook(url=user_config.WEBHOOK, content=msg)
            webhook.execute()

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def d(o, no):
        with open(f'test{no}.json', 'w', encoding='utf-8') as f:
            f.write(json.dumps(o, ensure_ascii=False, indent=2))

    @staticmethod
    def timestamp_to_str(ts: int) -> str:
        if ts < 0:
            return ''
        dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=TZ_OFFSET)))
        tztext = dt.strftime('%z')[:3]
        return ' • ' + dt.strftime('%Y/%m/%d %H:%M:%S ') + f'UTC{tztext}'

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
        if fmt:
            fmt = '\n' + fmt
        return fmt

    @staticmethod
    def parallel_map(lst, func) -> dict:
        parallel_results = {}
        with concurrent.futures.ThreadPoolExecutor() as executor:
            f = {executor.submit(func, i): i for i in lst}
            for future in concurrent.futures.as_completed(f):
                k = f[future]
                parallel_results[k] = future.result()
        return parallel_results


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
    def last(obj: dict, key: str) -> dict:
        return Jq.iterate(obj, key)[-1]


class Cache:
    db = []
    limit: int = 64  # to be determined with actual testing
    index: int = 0

    @staticmethod
    def put(imgid: str, image: Image) -> int:
        if not Cache.db:
            [Cache.db.append([None, None]) for _ in range(Cache.limit)]
        Cache.db[Cache.index][0] = imgid
        Cache.db[Cache.index][1] = image

        oi = Cache.index
        Cache.index += 1
        if Cache.index >= Cache.limit:
            Cache.index = 0

        return oi

    @staticmethod
    def get(imgfilename: str) -> Image:
        ii = imgfilename.split('-')
        if len(ii) != 2:
            return None
        imgindex, imgid = ii
        if not re.match('^[0-9]+$', imgindex):
            return None
        imgindex = int(imgindex)
        if None in Cache.db[imgindex] or Cache.db[imgindex][0] != imgid:
            return None
        return Cache.db[imgindex][1]


class Cookies:
    def __init__(self, fn: str):
        self.cookies: list = []

        if not os.path.isfile(fn):
            logging.warning('cookies.json not found, random shit will NOT work')
            return

        with open(fn) as f:
            self.cookies = json.load(f)
            logging.info(f'loaded {len(self.cookies)} cookies from {fn}')
        self.get_cookies()

    def is_valid_cookie(self, entry: dict) -> bool:
        return int(entry.get('expirationDate', 2**31)) > time.time()

    def get_cookies(self) -> dict[str, str]:
        if any([not self.is_valid_cookie(cookie) for cookie in self.cookies]):
            Utils.warn('@everyone cookies expired')
            return {}

        return {k['name']: k['value'] for k in self.cookies}


acc = Cookies('cookies.json')


class Auth:
    @staticmethod
    @lru_cache()
    def get_credentials():
        username = os.getenv('FACEBED_USERNAME')
        password_hash = os.getenv('FACEBED_PASSWORD_HASH')

        if not username or not password_hash:
            username = user_config.USERNAME
            password_hash = hashlib.sha256(user_config.PASSWORD.encode()).hexdigest()

        return username, password_hash

    @staticmethod
    def check_auth(username, password):
        return (username, hashlib.sha256(password.encode()).hexdigest()) == Auth.get_credentials()


class Story:
    author_name: str
    text: str
    image_links: list[str]
    video_links: list[str]
    url: str

    author_id: int
    attached_story: Self

    def __init__(self, story_json: dict):
        self.author_name = story_json['actors'][0]['name']
        self.text = story_json['message']['text'] if (story_json['message'] and 'text' in story_json['message']) else ''
        self.image_links = self.get_image_links_post_json(story_json)
        self.video_links = self.get_video_links(story_json)
        self.url = story_json['wwwURL']
        self.author_id = story_json['actors'][0]['id']

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
                subsets = [v for k, v in attachment_set.items() if k.endswith('subattachments')]
                max_imgage_count = len(max(subsets, key=lambda it: len(it['nodes']))['nodes'])
                subsets = [subset for subset in subsets if
                           len(subset['nodes']) == max_imgage_count and Jq.all(subset, 'viewer_image')]
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


class FacebedException(Exception):
    pass


class JsonParser:
    @staticmethod
    def get_headers() -> dict:
        headers =  {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Sec-Fetch-Site': 'none'
        }
        headers.update(Utils.get_ua())
        return headers


    @staticmethod
    def get_json_blocks(html_parser: BeautifulSoup, sort=True) -> list[str]:
        script_elements = html_parser.find_all('script', attrs={'type': 'application/json', 'data-content-len': True, 'data-sjs': True})
        if sort:
            script_elements.sort(key=lambda e: int(e.attrs['data-content-len']), reverse=True)
        return [e.text for e in script_elements]

    @staticmethod
    def get_post_json(html_parser: BeautifulSoup) -> dict:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'comment_rendering_instance' in json_block:  # TODO: add more robust detection
                bloc = json.loads(json_block)
                assert bloc
                return bloc
        raise FacebedException('cannot find post json')

    @staticmethod
    def get_group_name(html_parser: BeautifulSoup) -> str:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'group_member_profiles' in json_block and 'formatted_count_text' in json_block:
                group_json = json.loads(json_block)
                for group_object in Jq.all(group_json, 'group'):
                    if 'name' in group_object:
                        return group_object['name']
        return ''

    @staticmethod
    def get_interaction_counts(post_json: dict) -> [str, str, str]:
        assert post_json
        post_feedback = Jq.first(post_json, 'comet_ufi_summary_and_actions_renderer')
        assert post_feedback
        reactions = post_feedback['feedback']['i18n_reaction_count']
        shares = post_feedback['feedback']['i18n_share_count']
        comments = post_feedback['feedback']['comment_rendering_instance']['comments']['total_count']
        return str(reactions), str(comments), str(shares)

    @staticmethod
    def get_root_node(post_json: dict) -> dict:
        def work_normal_post() -> dict:
            data_blob = Jq.first(post_json, 'data')
            if 'comet_ufi_summary_and_actions_renderer' in data_blob:   # single photo
                return data_blob
            else:
                return data_blob['node']['comet_sections']

        def work_group_post() -> dict:
            hoisted_feed = Jq.first(post_json, 'group_hoisted_feed')
            comet_section = Jq.first(hoisted_feed, 'comet_sections')
            return comet_section

        methods: list[Callable[[], dict]] = [work_normal_post, work_group_post]

        for method in methods:
            try:
                ret = method()
                if ret:
                    return ret
                else:
                    continue
            except (StopIteration, KeyError):
                continue


        raise FacebedException('Cannot process post')

    @staticmethod
    def ensure_full_url(u: str) -> str:
        if u.startswith(WWWFB):
            return u
        else:
            return f'{WWWFB}/{u.removeprefix("/")}'

    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        http_response = requests.get(JsonParser.ensure_full_url(post_path),
                                     headers=JsonParser.get_headers(), cookies=acc.get_cookies())
        html_parser = BeautifulSoup(http_response.text, 'html.parser')

        post_json = JsonParser.get_root_node(JsonParser.get_post_json(html_parser))
        likes, cmts, shares = JsonParser.get_interaction_counts(post_json)
        # noinspection PyTypeChecker
        post_date = int(Jq.first(post_json['context_layout']['story']['comet_sections']['metadata'], 'creation_time'))
        post_json = post_json['content']['story']

        story = Story(post_json)
        post_url = story.url
        post_content = story.get_text()
        post_group_name = JsonParser.get_group_name(html_parser)
        post_author_name = story.author_name
        link_header = f'{post_author_name}' + (f' • {post_group_name}' if post_group_name else '')

        if story.author_id in user_config.BANNED_USER_IDS:
            return banned(post_url)

        # TODO: support normal /watch here
        return ParsedPost(link_header, post_content.strip(), story.image_links, post_url, post_date,
                          likes, cmts, shares, story.video_links)



class SinglePhotoParser:
    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'message_preferred_body' in json_block and 'container_story' in json_block:
                return Jq.first(json.loads(json_block), 'data')
        raise FacebedException('Cannot process post (cn)')

    @staticmethod
    def get_interactions_node(html_parser: BeautifulSoup) -> dict:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'comet_ufi_summary_and_actions_renderer' in json_block:
                return json.loads(json_block)
        raise FacebedException('Cannot process post (in)')

    @staticmethod
    def get_single_image(html_parser: BeautifulSoup) -> str:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'prefetch_uris_v2' in json_block:
                return str(Jq.first(json.loads(json_block), 'prefetch_uris_v2')[0]['uri'])
        raise FacebedException('cannot find single image')

    @staticmethod
    def process_post(post_path: str):
        http_response = requests.get(JsonParser.ensure_full_url(post_path),
                                     headers=JsonParser.get_headers(), cookies=acc.get_cookies())
        html_parser = BeautifulSoup(http_response.text, 'html.parser')
        content_node = SinglePhotoParser.get_content_node(html_parser)
        interaction_node = SinglePhotoParser.get_interactions_node(html_parser)

        post_text = content_node['message']['text'] if content_node['message'] and 'text' in content_node['message'] else ''
        post_author = content_node['owner']['name']
        post_date = content_node['created_time']
        likes, cmts, shares = JsonParser.get_interaction_counts(interaction_node)
        image_url = SinglePhotoParser.get_single_image(html_parser)

        return ParsedPost(post_author, post_text.strip(), [image_url], JsonParser.ensure_full_url(post_path),
                          post_date, likes, cmts, shares, [])


class ReelsParser:
    @staticmethod
    def get_video_link(html_parser: BeautifulSoup|None, user_node: dict = None) -> str:
        def work_node(node: dict) -> str:
            video_node = Jq.first(node, 'videoDeliveryLegacyFields')
            for key in ['browser_native_hd_url', 'browser_native_sd_url']:
                try:
                    video_link = Jq.first(video_node, key)
                    if not video_link:
                        continue
                    return str(video_link)
                except StopIteration:
                    pass
            raise FacebedException('Invalid reels link (vn)')

        if user_node:
            return work_node(user_node)

        # randomly breaks if sorted
        for json_block in JsonParser.get_json_blocks(html_parser, sort=False):
            if 'browser_native_hd_url' in json_block or 'browser_native_sd_url' in json_block:
                return work_node(json.loads(json_block))

        raise FacebedException('Invalid reels link (vn)')


    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'short_form_video_context' in json_block and 'creation_story' in json_block:
                return Jq.first(json.loads(json_block), 'creation_story')
        raise FacebedException('Invalid reels link (cn)')

    @staticmethod
    def get_reaction_counts(html_parser: BeautifulSoup, is_ig: bool, video_id: str) -> tuple[str, str, str]:
        blocks: list[dict] = []
        for json_block in JsonParser.get_json_blocks(html_parser, sort=False):
            if 'viewer_feedback_reaction_key' in json_block:
                block = json.loads(json_block)
                if any([vid == video_id for vid in Jq.all(block, 'id')]):
                    blocks.append(block)

        if len(blocks) == 0:
            raise FacebedException('Cannot process post (cn)')

        # assuming the last one contains ig info
        block = blocks[0]
        first_fb = Jq.first(block, 'feedback')
        last_fb = Jq.last(block, 'feedback')

        if 'cross_universe_feedback_info' in str(first_fb):
            first_fb, last_fb = last_fb, first_fb

        ig_cmts = last_fb['cross_universe_feedback_info']['ig_comment_count']
        likes = first_fb['unified_reactors']['count']
        cmts = ig_cmts if is_ig else last_fb['total_comment_count']
        shares = last_fb['share_count_reduced'] # TODO: investigate why it's "reduced"

        return Utils.human_format(likes), Utils.human_format(cmts), Utils.human_format(shares)


    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        http_response = requests.get(JsonParser.ensure_full_url(post_path),
                                     headers=JsonParser.get_headers())
        html_parser = BeautifulSoup(http_response.text, 'html.parser')
        content_node = ReelsParser.get_content_node(html_parser)
        sfvc = content_node['short_form_video_context']

        video_link = ReelsParser.get_video_link(html_parser)
        video_id = content_node['video']['id']
        is_ig = content_node['video']['owner']['__typename'].startswith('InstagramUser')
        op_name = ('📷 @' if is_ig else '') + sfvc['video_owner']['username' if is_ig else 'name']
        post_url = sfvc['shareable_url']
        post_date = content_node['creation_time']
        post_text = content_node['message']['text'] if content_node['message'] is not None else ''
        likes, cmts, shares = ReelsParser.get_reaction_counts(html_parser, is_ig, video_id)

        if sfvc['video_owner']['id'] in user_config.BANNED_USER_IDS:
            return banned(post_url)

        return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


class VideoWatchParser:
    # excluding group post video since they are handled by jsonparser
    @staticmethod
    def get_op_name(html_parser: BeautifulSoup) -> str:
        for json_block in JsonParser.get_json_blocks(html_parser, sort=False):
            if 'is_eligible_for_subscription_gift_purchase' in json_block:
                bloc = json.loads(json_block)
                return Jq.first(bloc, 'owner')['name']
        raise FacebedException('Invalid watch link (opn)')


    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'comment_rendering_instance' in json_block and 'video_view_count_renderer' in json_block:
                return Jq.first(json.loads(json_block), 'result')['data']
        raise FacebedException('Invalid watch link (cn)')

    @staticmethod
    def get_date(html_parser: BeautifulSoup) -> int:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if 'creation_time' in json_block:
                #   noinspection PyTypeChecker
                return int(Jq.first(json.loads(json_block), 'creation_time'))
        raise FacebedException('cannot find date')

    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        http_response = requests.get(JsonParser.ensure_full_url(post_path),
                                     headers=JsonParser.get_headers(), cookies=acc.get_cookies())
        html_parser = BeautifulSoup(http_response.text, 'html.parser')
        content_node = VideoWatchParser.get_content_node(html_parser)

        video_link = ReelsParser.get_video_link(html_parser)

        post_url = JsonParser.ensure_full_url(post_path)
        op_name = VideoWatchParser.get_op_name(html_parser)
        post_text = content_node['title']['text'] if content_node['title'] else ''
        likes = Utils.human_format(content_node['feedback']['reaction_count']['count'])
        shares = 'null'
        cmts = Utils.human_format(content_node['feedback']['total_comment_count'])
        post_date = VideoWatchParser.get_date(html_parser)

        return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


def format_error_message_embed(msg: str, original_url: str) -> str:
    return Utils.prettify(template(f'''<!DOCTYPE html>
<html lang="">
<head>
<meta charset="UTF-8" />
    <title>{get_credit()}</title>
    <meta name="theme-color" content="#0866ff" />
    <meta property="og:title" content="{get_credit()}"/>
    <meta property="og:description" content="{msg}"/>
    <meta http-equiv="refresh" content="0;url={{{{original_url}}}}"/>
</head>
</html>''', original_url=original_url))


def is_facebook_url(url: str) -> bool:
    wwwfb = f'{WWWFB}/'
    username_pattern = '[a-zA-Z0-9-._]*'  # also covers /watch
    full_url = f'{wwwfb}{url}'
    parsed_url = urlparse(full_url)

    is_group_post = re.match(f'^/groups/{username_pattern}', parsed_url.path)
    is_permalink = parsed_url.path.startswith('/permalink.php')
    is_story = parsed_url.path.startswith('/story.php')
    is_post = re.match(f'/{username_pattern}/posts', parsed_url.path)
    is_photo = parsed_url.path.startswith('/photo')

    return is_permalink or is_post or is_story or is_photo or is_group_post


@lru_cache
def get_font_renderer() -> FreeTypeFont:
    try:
        font_renderer = ImageFont.truetype(os.path.join(os.path.dirname(__file__), 'text_mode_assets', 'SFProDisplay-Medium.ttf'), 20)
    except IOError:
        font_renderer = ImageFont.load_default()
    return font_renderer


def render_text_mode_post(post_text: str) -> Image:
    start_time = time.time()
    canvas_width = 400 if len(post_text) <= 200 else 800
    margin = 20
    background_color = (24, 25, 26)
    text_color = (228, 230, 235)

    font_text = get_font_renderer()

    # -- Measure and wrap the post text
    temp_image = Image.new('RGB', (canvas_width, 100), background_color)
    temp_draw = ImageDraw.Draw(temp_image)

    max_text_width = canvas_width - 2 * margin
    wrapped_text = []
    for line in post_text.split('\n'):
        words = line.split(' ')
        current_line = ''
        for word in words:
            test_line = f'{current_line} {word}'.strip()
            text_bbox = temp_draw.textbbox((0, 0), test_line, font=font_text)
            text_width = text_bbox[2] - text_bbox[0]
            if text_width <= max_text_width:
                current_line = test_line
            else:
                wrapped_text.append(current_line)
                current_line = word
        wrapped_text.append(current_line)
    wrapped_text = '\n'.join(wrapped_text)

    # -- Calculate dynamic height
    text_bbox = temp_draw.multiline_textbbox((0, 0), wrapped_text, font=font_text)
    text_height = text_bbox[3] - text_bbox[1]
    canvas_height = int(text_height + 2 * margin)

    # -- Create canvas
    canvas = Image.new('RGB', (canvas_width, canvas_height), background_color)

    # -- Post content
    text_x = margin
    text_y = margin
    with Pilmoji(canvas) as pilmoji:
        pilmoji.text((text_x, text_y), wrapped_text, font=font_text, fill=text_color)

    logging.info(f'took {round(time.time() - start_time, 2)} seconds to render {len(post_text)} characters')
    return canvas


def format_reel_post_embed(post: ParsedPost) -> str:
    def get_video_meta_tag(link: str) -> str:
        return '\n'.join([
            f'<meta property="twitter:player:stream" content="{link}"/>',
            f'<meta property="og:video" content="{link}"/>'
            f'<meta property="og:video:secure_url" content="{link}"/>'
        ])

    video_meta_tags = '\n'.join([get_video_meta_tag(vu) for vu in post.video_links])
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)
    color = '#0866ff'

    return Utils.prettify(template(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{{{{opname}}}}"/>
            <meta property="og:site_name" content="{get_credit()}{reaction_str}"/>
            <meta property="og:url" content="{post.url}"/>
            <meta property="og:video:type" content="video/mp4"/>
            <meta property="twitter:player:stream:content_type" content="video/mp4"/>

            {video_meta_tags}

            <link rel="canonical" href="{post.url}"/>
            <meta http-equiv="refresh" content="0;url={post.url}"/>
            <meta name="twitter:card" content="player"/>
            <meta name="theme-color" content="{color}"/>
        </head>
        </html>''', opname=post.author_name, likes=post.likes, cmts=post.comments, shares=post.shares))


def format_full_post_embed(post: ParsedPost) -> str:
    if post.video_links:
        return format_reel_post_embed(post)
    image_links = post.image_links
    image_counter = f'\ncontains 4+ images' if len(image_links) > 4 else ''
    image_links = image_links[:4]
    image_meta_tags = '\n'.join([f'<meta property="og:image" content="{iu}"/>' for iu in image_links])
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)

    # TODO: organize and duplicate the neccessary tags
    return Utils.prettify(template(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{{{{opname}}}}"/>
            <meta property="og:description" content="{{{{content}}}}"/>
            <meta property="og:site_name" content="{get_credit()}{reaction_str}{{{{post_date}}}}{image_counter}"/>
            <meta property="og:url" content="{post.url}"/>
            {image_meta_tags}
            <link rel="canonical" href="{post.url}"/>
            <meta http-equiv="refresh" content="0;url={post.url}"/>
            <meta name="twitter:card" content="summary_large_image"/>
            <meta name="theme-color" content="#0866ff"/>
        </head>
        </html>''', opname=post.author_name, content=post.text[:1024],
                    likes=post.likes, cmts=post.comments, shares=post.shares,
                    post_date=Utils.timestamp_to_str(post.date)))


def format_text_post_embed(post: ParsedPost) -> str:
    def ll():
        return ''.join([random.choice(['l', 'I']) for _ in range(32)])

    text_mode_image = render_text_mode_post(post_text=post.text.strip())
    iid = ll()
    img_index = Cache.put(iid, text_mode_image)

    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)

    return template(f'''<!DOCTYPE html>
    <html lang="">
    <head>
        <title>{get_credit()}</title>
        <meta charset="UTF-8"/>
        <meta property="og:title" content="{{{{opname}}}}"/>
        <meta property="og:site_name" content="{get_credit()}{reaction_str}{{{{post_date}}}}"/>
        <meta property="og:url" content="{post.url}"/>
        <meta property="og:image" content="/txtimg/{img_index}-{iid}.png"/>
        <link rel="canonical" href="{post.url}"/>
        <meta http-equiv="refresh" content="0;url={post.url}"/>
        <meta content="summary_large_image" name="twitter:card"/>
        <meta name="theme-color" content="#0866ff"/>
    </head>
    </html>''', opname=post.author_name, likes=post.likes, cmts=post.comments, shares=post.shares,
                    post_date=Utils.timestamp_to_str(post.date))


def process_post(post_path: str, text_mode: bool) -> str:
    post_path = post_path.removeprefix(WWWFB).removeprefix('/')
    parsed_post = JsonParser.process_post(post_path)
    if type(parsed_post) == ParsedPost:
        if text_mode:
            return format_text_post_embed(parsed_post)
        else:
            return format_full_post_embed(parsed_post)
    else:
        return format_error_message_embed('Cannot process post', f'{WWWFB}/{post_path}')


def process_single_photo(post_path: str, text_mode: bool) -> str:
    parsed_post = SinglePhotoParser.process_post(post_path)
    if type(parsed_post) == ParsedPost:
        if text_mode:
            return format_text_post_embed(parsed_post)
        else:
            return format_full_post_embed(parsed_post)
    return format_error_message_embed('Cannot process post', f'{WWWFB}/{post_path}')


@app.route('/txtimg/<path:path>')
def txtimg(path: str):
    imgid = path
    image = Cache.get(imgid.removesuffix('.png'))
    if image is None:
        response.status = 404
        return 'invalid request'

    bytes_array = BytesIO()
    image.save(bytes_array, format='PNG', quality=100)
    response.content_type = 'image/png'
    response.set_header('Cache-Control', f'public, max-age={int(timedelta(hours=24).total_seconds())}')
    return bytes_array.getvalue()


@app.route('/<path:path>')
def index(path: str):
    if request.query_string:
        path += f'?{request.query_string}'
    use_text_mode = False
    if path.endswith('/text'):
        path = path.removesuffix('/text')
        use_text_mode = True

    if 'type' in request.query.dict and '3' in request.query.dict['type']:
        return format_error_message_embed('images in comment are not supported', 'https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400')

    if re.match('^(/)?share/v/.*', path):
        path = Utils.resolve_share_link(path)
        if not path:
            return format_error_message_embed('Share link (v) redirected to nowhere.', f'{WWWFB}/{path}')
        search = re.search(r'/videos/(\d+)', path)
        if search:
            video_id = search.group(1)
            path = f'watch/?v={video_id}'

    if re.match('^(/)?share/([pr]/)?[a-zA-Z0-9-._]*(/)?', path):
        path = Utils.resolve_share_link(path)
        if not path:
            return format_error_message_embed('Share link redirected to nowhere.', f'{WWWFB}/{path}')

    if re.match(f'^/?reel/[0-9]+', path):
        return format_reel_post_embed(ReelsParser.process_post(path))

    if re.match('^/*photo/*$', urlparse(path).path):
        return process_single_photo(path, use_text_mode)

    if re.match('^/*watch', urlparse(path).path):
        return format_reel_post_embed(VideoWatchParser.process_post(path))

    if is_facebook_url(path):
        return process_post(path, use_text_mode)
    else:
        return format_error_message_embed('This is not a Facebook link.', 'https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400')


@app.route('/favicon.ico')
def favicon():
    response.content_type = 'image/x-icon'
    return static_file('favicon.ico', root='./assets')


@app.route('/banner.png')
def favicon():
    response.content_type = 'image/png'
    return static_file('banner.png', root='./assets')


@app.route('/update')
def update():
    def get_commit_id() -> str:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('utf-8').strip()

    def run_start_command():
        time.sleep(1)
        from start import START_COMMANDS
        for command in START_COMMANDS[:-1]:
            subprocess.run(command)
        os.execv(START_COMMANDS[-1][0], START_COMMANDS[-1])

    if not user_config.ENABLE_REMOTE_UPDATER:
        logging.info('update disabled')
        response.status = 401
        return 'remote updating disabled'

    auth = request.auth
    if auth and Auth.check_auth(auth[0], auth[1]):
        logging.info('update request authenticated')
        old_commit_id = get_commit_id()
        subprocess.run(['git', 'pull'])
        new_commit_id = get_commit_id()
        threading.Thread(target=run_start_command, daemon=True).start()
        return f'updating from <code>{old_commit_id}</code> to <code>{new_commit_id}</code>'
    else:
        logging.error('update request not authenticated')
        time.sleep(random.randint(100, 1000) / 1000)
        response.status = 401
        response.headers['WWW-Authenticate'] = 'Basic realm="Login Required"'
        return 'Access Denied: Can\'t remotely update facebed!'


@app.route('/')
def root():
    with open('assets/index.html', encoding='utf-8') as f:
        return f.read().replace('{|CREDIT|}', get_credit())


def log_to_logger(fn):
    @wraps(fn)
    def _log_to_logger(*argsz, **kwargs):
        actual_response = fn(*argsz, **kwargs)
        logging.info('%s %s %s %s' % (request.remote_addr, request.method, request.url, response.status))
        return actual_response

    return _log_to_logger


def main():
    global TZ_OFFSET

    parser = argparse.ArgumentParser(description='Facebook embed server')
    parser.add_argument('-p', '--port', type=int, default=9812, help='port number')
    parser.add_argument('-H', '--host', default='0.0.0.0', help='host address')
    parser.add_argument('-z', '--timezone', required=True, type=int, help='time zone offset')
    args = parser.parse_args()

    TZ_OFFSET = args.timezone
    if TZ_OFFSET < -12 or TZ_OFFSET > 14:
        logging.critical('invalid timezone offset')
        exit(1)

    if not user_config.ENABLE_REMOTE_UPDATER:
        logging.info('disabling remote update')

    if sys.version_info.minor < 12:
        logging.error('python 3.12+ required, see https://docs.python.org/3.12/whatsnew/3.12.html#pep-701-syntactic-formalization-of-f-strings')
        exit(1)

    logging.info(f'listening on {args.host}:{args.port}')
    app.install(log_to_logger)
    app.run(host=args.host, port=args.port, quiet=True)


if __name__ == '__main__':
    main()
