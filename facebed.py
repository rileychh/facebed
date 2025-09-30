import argparse
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
from datetime import timezone, timedelta, datetime
from functools import wraps
from typing import Self, Callable
from urllib.parse import quote as _quote_
from html import escape
from urllib.parse import urlparse

import stealth_requests as requests
import requests as rq
import yaml
from bottle import Bottle, request, response, static_file
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook
from yattag import indent

CONFIG_STR = '''
host: 0.0.0.0
port: 9812
timezone: 7
banned_users: []
banned_notifier_webhook: ''
'''.strip()

config: dict = {}
default_config: dict = yaml.safe_load(io.StringIO(CONFIG_STR))
app: Bottle = Bottle()

WWWFB = 'https://www.facebook.com'
TZ_OFFSET: int = 0
ALLOW_UPDATE = True
logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(msg)s', level=logging.INFO)


def quote(s: str) -> str:
    return "".join([
        _quote_(char) if char in r"<>\"'#%{}[]|\\^~`" else char
        for char in s
    ])

def get_credit() -> str:
    return "facebed by pi.kt (Riley's fork)"


class Utils:
    @staticmethod
    def resolve_share_link(path: str) -> str:
        # cookies not needed to resolve share links
        head_request = rq.head(f'{WWWFB}/{path}', headers=JsonParser.get_headers())
        if head_request.next is None or head_request.next.url.startswith('https://www.facebook.com/share'):
            return ''
        path = head_request.next.url.removeprefix(f'{WWWFB}/')
        # print(path)
        return path


    @staticmethod
    def prettify(txt: str) -> str:
        return indent(txt, indentation ='    ', newline = '\n', indent_text = True)

    @staticmethod
    def warn(msg: str):
        def worker():
            wh = config['banned_notifier_webhook']
            if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
                return
            try:
                webhook = DiscordWebhook(url=config['banned_notifier_webhook'], content=msg)
                webhook.execute()
            except Exception:
                logging.warning(f'failed to warn about "{msg}"')

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
        if num is int or re.match('^[0-9]+$', str(num)):
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
    def last(obj: dict, key: str) -> dict:
        return Jq.iterate(obj, key)[-1]


class Cookies:
    def __init__(self, fn: str):
        self.cookies: list = []

        if not os.path.isfile(fn):
            logging.warning('cookies.json not found, non incognito-viewable posts will NOT work')
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
                subsets = [v for k, v in attachment_set.items() if k.endswith('subattachments') and 'nodes' in v]
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
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'priority': 'u=0, i',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
        }

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
            if 'i18n_reaction_count' in json_block:  # TODO: add more robust detection
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
    def get_interaction_counts(post_json: dict) -> tuple[str, str, str]:
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

        if story.author_id in config['banned_users']:
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
            if 'browser_native_' in json_block and 'creation_story' in json_block:
                return Jq.first(json.loads(json_block), 'creation_story')
        raise FacebedException('Invalid reels link (cn)')

    @staticmethod
    def get_reaction_counts(html_parser: BeautifulSoup, is_ig: bool, video_id: str) -> tuple[str, str, str]:
        blocks: list[dict] = []
        for json_block in JsonParser.get_json_blocks(html_parser, sort=False):
            if 'unified_reactors' in json_block:
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

        video_link = ReelsParser.get_video_link(html_parser)
        video_id = content_node['id']
        owner_info = content_node['short_form_video_context']['video_owner']
        is_ig = owner_info['__typename'].startswith('InstagramUser')
        op_name = ('📷 @' if is_ig else '') + owner_info['username' if is_ig else 'name']
        post_url = content_node['short_form_video_context']['shareable_url']
        post_date = content_node['creation_time']
        post_text = content_node['message']['text']

        likes, cmts, shares = ReelsParser.get_reaction_counts(html_parser, is_ig, video_id)

        if owner_info['id'] in config['banned_users']:
            return banned(post_url)

        return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


class VideoWatchParser:
    # excluding group post video since they are handled by jsonparser
    @staticmethod
    def get_op_name(html_parser: BeautifulSoup) -> str:
        for json_block in JsonParser.get_json_blocks(html_parser, sort=False):
            if 'is_additional_profile_plus' in json_block:
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


def format_error_message_embed(original_url: str) -> str:
    return Utils.prettify(f'''<!DOCTYPE html>
<html lang="">
<head>
<meta charset="UTF-8" />
    <meta name="theme-color" content="#2c3048f" />
    <meta property="og:title" content="Log in or sign up to view"/>
    <meta property="og:description" content="See posts, photos and more on Facebook.\nIf viewable in incognito report to git.facebed.com."/>
    <meta http-equiv="refresh" content="0;url={quote(original_url)}"/>
</head>
</html>''')


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


def format_reel_post_embed(post: ParsedPost) -> str:
    def get_video_meta_tag(link: str) -> str:
        return '\n'.join([
            f'<meta property="twitter:player:stream" content="{link}"/>',
            f'<meta property="og:video" content="{link}"/>'
            f'<meta property="og:video:secure_url" content="{link}"/>'
        ])

    video_meta_tags = '\n'.join([get_video_meta_tag(vu) for vu in post.video_links])
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)
    post_date = Utils.timestamp_to_str(post.date)
    color = '#0866ff'

    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:site_name" content="{get_credit()}\n{post_date}\n{reaction_str}"/>
            <meta property="og:url" content="{quote(post.url)}"/>
            <meta property="og:video:type" content="video/mp4"/>
            <meta property="twitter:player:stream:content_type" content="video/mp4"/>

            {video_meta_tags}

            <link rel="canonical" href="{quote(post.url)}"/>
            <meta http-equiv="refresh" content="0;url={quote(post.url)}"/>
            <meta name="twitter:card" content="player"/>
            <meta name="theme-color" content="{color}"/>
        </head>
        </html>''')


def format_full_post_embed(post: ParsedPost) -> str:
    if post.video_links:
        return format_reel_post_embed(post)
    image_links = post.image_links
    image_counter = '\ncontains 4+ images' if len(image_links) > 4 else ''
    image_links = image_links[:4]
    image_meta_tags = '\n'.join([f'<meta property="og:image" content="{iu}"/>' for iu in image_links])
    post_date = Utils.timestamp_to_str(post.date)
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)

    # TODO: organize and duplicate the neccessary tags
    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{get_credit()}\n{post_date}\n{reaction_str}{image_counter}"/>
            <meta property="og:url" content="{quote(post.url)}"/>
            {image_meta_tags}
            <link rel="canonical" href="{quote(post.url)}"/>
            <meta http-equiv="refresh" content="0;url={quote(post.url)}"/>
            <meta name="twitter:card" content="summary_large_image"/>
            <meta name="theme-color" content="#0866ff"/>
        </head>
        </html>''')


def process_post(post_path: str) -> str:
    post_path = post_path.removeprefix(WWWFB).removeprefix('/')
    parsed_post = JsonParser.process_post(post_path)
    if type(parsed_post) == ParsedPost:
        return format_full_post_embed(parsed_post)
    return format_error_message_embed(f'{WWWFB}/{post_path}')


def process_single_photo(post_path: str) -> str:
    parsed_post = SinglePhotoParser.process_post(post_path)
    if type(parsed_post) == ParsedPost:
        return format_full_post_embed(parsed_post)
    return format_error_message_embed(f'{WWWFB}/{post_path}')


@app.route('/<path:path>')
def index(path: str):
    if request.query_string:
        path += f'?{request.query_string}'

    if 'type' in request.query.dict and '3' in request.query.dict['type']:
        return format_error_message_embed(f'{WWWFB}/{path}')

    try:
        if re.match('^(/)?share/v/.*', path):
            path = Utils.resolve_share_link(path)
            if not path:
                return format_error_message_embed(f'{WWWFB}/{path}')

        if re.match('^(/)?share/([pr]/)?[a-zA-Z0-9-._]*(/)?', path):
            path = Utils.resolve_share_link(path)
            if not path:
                return format_error_message_embed(f'{WWWFB}/{path}')

        search = re.search(r'/videos/(\d+).*', path)
        if search:
            video_id = search.group(1)
            path = f'reel/{video_id}'

        if re.match('^/?reel/[0-9]+', path):
            return format_reel_post_embed(ReelsParser.process_post(path))

        if re.match('^/*photo/*$', urlparse(path).path):
            return process_single_photo(path)

        if re.match('^/*watch', urlparse(path).path):
            return format_reel_post_embed(VideoWatchParser.process_post(path))

        if is_facebook_url(path):
            return process_post(path)
        else:
            return format_error_message_embed('https://git.facebed.com')


    except FacebedException:
        print(traceback.format_exc())
        return format_error_message_embed(f'{WWWFB}/{path}')
    except Exception:
        print(traceback.format_exc())
        return format_error_message_embed(f'{WWWFB}/{path}')


@app.route('/favicon.ico')
def favicon():
    response.content_type = 'image/x-icon'
    return static_file('favicon.ico', root='./assets')


@app.route('/banner.png')
def banner():
    response.content_type = 'image/png'
    return static_file('banner.png', root='./assets')


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

    logging.info(f'listening on {config['host']}:{config['port']}')
    app.install(log_to_logger)
    app.run(host=config['host'], port=config['port'], quiet=True)


if __name__ == '__main__':
    main()
