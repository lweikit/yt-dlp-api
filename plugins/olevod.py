import base64
import hashlib
import json
import re
import time

from yt_dlp.aes import aes_cbc_decrypt_bytes, unpad_pkcs7
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    clean_html,
    float_or_none,
    int_or_none,
    js_to_json,
    traverse_obj,
    url_or_none,
    urljoin,
)


class OlevodIE(InfoExtractor):
    IE_NAME = 'olevod'
    _API_BASE = 'https://api.olelive.com'
    _SITE_BASE = 'https://www.olevod.com'
    _VALID_URL = r'''(?x)
        https?://
        (?:www\.)?olevod\.com/
        (?:
            index\.php/vod/play/id/(?P<legacy_id>\d+)/sid/(?P<sid>\d+)/nid/(?P<nid>\d+)\.html
          |
            player/vod/(?P<content_type>\d+)-(?P<new_id>\d+)-(?P<episode_id>\d+)\.html
        )
    '''
    _TESTS = [{
        'url': 'https://olevod.com/index.php/vod/play/id/81328/sid/1/nid/1.html',
        'info_dict': {
            'id': '81328',
            'ext': 'mp4',
            'series': '重案解密【粤语】',
            'episode': '01集',
            'episode_number': 1,
        },
        'params': {
            'skip_download': True,
        },
    }, {
        'url': 'https://www.olevod.com/player/vod/2-81328-1.html',
        'info_dict': {
            'id': '81328-1',
            'ext': 'mp4',
            'title': '重案解密【粤语】_第01集',
            'series': '重案解密【粤语】',
            'episode': '第01集',
            'episode_number': 1,
        },
        'params': {
            'skip_download': True,
        },
    }]

    @staticmethod
    def _clean_title(title):
        if not title:
            return title
        return re.sub(r'\s*[-|_]\s*(?:欧乐影院.*|高清|完结)\s*$', '', title).strip()

    @staticmethod
    def _make_vv(ts=None):
        ts = str(int_or_none(ts) or int(time.time()))
        bits = ['', '', '', '']
        for char in ts:
            encoded = format(ord(char), 'b')
            bits[0] += encoded[2:3]
            bits[1] += encoded[3:4]
            bits[2] += encoded[4:5]
            bits[3] += encoded[5:]
        inserts = []
        for part in bits:
            value = format(int(part, 2), 'x') if part else ''
            if len(value) == 2:
                value = f'0{value}'
            elif len(value) == 1:
                value = f'00{value}'
            elif len(value) == 0:
                value = '000'
            inserts.append(value)
        digest = hashlib.md5(ts.encode()).hexdigest()
        return ''.join((
            digest[:3], inserts[0],
            digest[6:11], inserts[1],
            digest[14:19], inserts[2],
            digest[22:27], inserts[3],
            digest[30:],
        ))

    @classmethod
    def _api_headers(cls, referer=None):
        return {
            'Origin': cls._SITE_BASE,
            'Referer': referer or f'{cls._SITE_BASE}/',
        }

    @staticmethod
    def _extract_episode_title(title):
        if not title:
            return None, None
        episode = re.search(r'(第\s*(\d+)\s*集)$', title)
        if episode:
            return episode.group(1).replace(' ', ''), int_or_none(episode.group(2))
        return None, None

    def _decrypt_api_data(self, data):
        if not isinstance(data, str):
            return data
        now = int(time.time())
        for offset in (0, 86400, -86400):
            date_str = time.strftime('%Y-%m-%d', time.localtime(now + offset))
            key = hashlib.md5(date_str.encode()).hexdigest()[8:24].encode()
            try:
                decrypted = unpad_pkcs7(aes_cbc_decrypt_bytes(base64.b64decode(data), key, key)).decode()
                return json.loads(decrypted)
            except Exception:
                continue
        raise ExtractorError('Unable to decrypt Olevod API response', expected=True)

    def _extract_new_api_data(self, video_id, url):
        response = self._download_json(
            f'{self._API_BASE}/v1/pub/vod/detail/{video_id}/true',
            video_id, query={'_vv': self._make_vv()},
            headers=self._api_headers(),
            note='Downloading playback metadata')
        if traverse_obj(response, 'code') != 0:
            raise ExtractorError(
                f'Olevod API error: {traverse_obj(response, "msg") or "unknown"}',
                expected=True)
        return self._decrypt_api_data(response.get('data'))

    @staticmethod
    def _join_title(*parts):
        return '_'.join(filter(None, parts))

    def _extract_new_format(self, url, video_id, episode_id):
        webpage = self._download_webpage(url, video_id)
        api_data = self._extract_new_api_data(video_id, url)
        entries = traverse_obj(api_data, ('urls', lambda _, v: isinstance(v, dict)))
        if not entries:
            raise ExtractorError('No playback entries found in detail API response', expected=True)

        selected = next((
            entry for entry in entries if int_or_none(entry.get('index')) == episode_id
        ), None)
        if not selected and episode_id and 0 < episode_id <= len(entries):
            selected = entries[episode_id - 1]
        if not selected:
            raise ExtractorError(f'Unable to find episode {episode_id}', expected=True)

        m3u8_url = (
            url_or_none(selected.get('url'))
            or traverse_obj(selected, ('vip_urls', 0, 'url', {url_or_none})))
        if not m3u8_url:
            raise ExtractorError('No HLS URL found in selected playback entry', expected=True)

        formats, subtitles = self._extract_m3u8_formats_and_subtitles(
            m3u8_url, f'{video_id}-{episode_id}', ext='mp4', fatal=True,
            headers=self._api_headers(url))

        raw_title = (
            traverse_obj(api_data, ('name', {str}))
            or self._html_search_meta(['og:title', 'twitter:title'], webpage, default=None)
            or self._html_extract_title(webpage, default=None)
            or f'Olevod video {video_id}'
        )
        title = self._clean_title(raw_title)
        episode, episode_number = self._extract_episode_title(selected.get('title'))
        is_series = traverse_obj(api_data, ('typeId1Name', {str})) == '连续剧' or episode is not None
        if episode_number is None and is_series:
            episode_number = int_or_none(selected.get('index'))
        series = title if is_series else None
        display_title = self._join_title(series or title, episode) if episode else title

        thumb_path = traverse_obj(api_data, ('picThumb', {str})) or traverse_obj(api_data, ('pic', {str}))
        thumbnail = url_or_none(thumb_path) or (urljoin('https://static.olelive.com/', thumb_path) if thumb_path else None)
        categories = [cat for cat in (
            traverse_obj(api_data, ('typeId1Name', {str})),
            traverse_obj(api_data, ('typeIdName', {str})),
        ) if cat]

        return {
            'id': f'{video_id}-{episode_id}',
            'title': display_title,
            'series': series,
            'episode': episode,
            'episode_number': episode_number,
            'description': clean_html(
                traverse_obj(api_data, ('content', {str}))
                or traverse_obj(api_data, ('blurb', {str}))
                or self._html_search_meta(['description', 'og:description'], webpage, default=None)),
            'thumbnail': thumbnail,
            'categories': categories or None,
            'cast': [name.strip() for name in (traverse_obj(api_data, ('actor', {str})) or '').split('/') if name.strip()],
            'creators': [name.strip() for name in (traverse_obj(api_data, ('director', {str})) or '').split('/') if name.strip()],
            'language': traverse_obj(api_data, ('lang', {str})),
            'release_year': int_or_none(traverse_obj(api_data, ('year', {str}))),
            'average_rating': float_or_none(traverse_obj(api_data, ('score', {str, int, float}))),
            'view_count': int_or_none(traverse_obj(api_data, ('hits', {int, str}))),
            'comment_count': int_or_none(traverse_obj(api_data, ('commentTotal', {int, str}))),
            'timestamp': int_or_none(traverse_obj(api_data, ('timeAdd', {int, str}))),
            'formats': formats,
            'subtitles': subtitles,
            'http_headers': self._api_headers(url),
        }

    def _extract_player_data(self, webpage, video_id):
        return self._search_json(
            r'var\s+player_aaaa\s*=',
            webpage,
            'player data',
            video_id,
            transform_source=js_to_json,
        )

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        video_id = match.group('legacy_id') or match.group('new_id')
        if match.group('new_id'):
            return self._extract_new_format(url, video_id, int_or_none(match.group('episode_id')) or 1)

        webpage = self._download_webpage(url, video_id)
        player_data = self._extract_player_data(webpage, video_id)

        m3u8_url = traverse_obj(player_data, ('url', {url_or_none}))
        if not m3u8_url:
            raise ExtractorError('No HLS URL found in player data', expected=True)

        if traverse_obj(player_data, ('encrypt',)) not in (0, '0', None):
            raise ExtractorError('Unsupported encrypted player payload', expected=True)

        formats, subtitles = self._extract_m3u8_formats_and_subtitles(
            m3u8_url, video_id, ext='mp4', fatal=True, headers={'Referer': url})

        title = (
            self._html_search_meta(['og:title', 'twitter:title'], webpage, default=None)
            or self._html_extract_title(webpage, default=None)
            or f'Olevod video {video_id}'
        )

        episode = self._search_regex(r'[第_ ](\d+集)', title, 'episode', default=None)
        episode_number = int_or_none(self._search_regex(
            r'[第_ ](\d+)集', title, 'episode number', default=None))
        series = self._search_regex(
            r'^(.*?)(?:[_ ]第\d+集)?(?:\s*[-|_]\s*欧乐影院.*)?$',
            title, 'series', default=None)

        return {
            'id': video_id,
            'title': self._clean_title(title),
            'series': self._clean_title(series),
            'episode': episode,
            'episode_number': episode_number,
            'formats': formats,
            'subtitles': subtitles,
            'http_headers': {'Referer': url},
        }


class OlevodSeriesIE(InfoExtractor):
    IE_NAME = 'olevod:series'
    _API_BASE = OlevodIE._API_BASE
    _SITE_BASE = OlevodIE._SITE_BASE
    _VALID_URL = r'''(?x)
        https?://(?:www\.)?olevod\.com/
        (?:
            detail/(?P<id>\d+)\.html
          |
            index\.php/vod/detail/id/(?P<legacy_id>\d+)\.html
          |
            vod/detail/(?P<alt_id>\d+)\.html
        )
    '''
    _TESTS = [{
        'url': 'https://www.olevod.com/detail/81328.html',
        'info_dict': {
            'id': '81328',
            'title': '重案解密【粤语】',
        },
        'playlist_count': 12,
    }]

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        video_id = match.group('id') or match.group('legacy_id') or match.group('alt_id')

        response = self._download_json(
            f'{self._API_BASE}/v1/pub/vod/detail/{video_id}/true',
            video_id, query={'_vv': OlevodIE._make_vv()},
            headers=OlevodIE._api_headers(),
            note='Downloading series metadata')
        if traverse_obj(response, 'code') != 0:
            raise ExtractorError(
                f'Olevod API error: {traverse_obj(response, "msg") or "unknown"}',
                expected=True)

        api_data = response.get('data')
        if isinstance(api_data, str):
            api_data = OlevodIE._decrypt_api_data(self, api_data)

        series_name = OlevodIE._clean_title(traverse_obj(api_data, ('name', {str})))
        entries_data = traverse_obj(api_data, ('urls', lambda _, v: isinstance(v, dict)))

        if not entries_data:
            raise ExtractorError('No episodes found', expected=True)

        type_id = traverse_obj(api_data, ('typeId1', {int_or_none})) or 2
        entries = []
        for entry in entries_data:
            idx = int_or_none(entry.get('index')) or (len(entries) + 1)
            episode_url = f'{self._SITE_BASE}/player/vod/{type_id}-{video_id}-{idx}.html'
            entries.append(self.url_result(episode_url, OlevodIE, f'{video_id}-{idx}'))

        return self.playlist_result(entries, video_id, series_name)
