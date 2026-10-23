"""Offline tests for build_playlist.py.

Run from the repository root:
    python -m unittest discover -s tests -v

Nothing here touches the internet: the iptv-org lists are replaced with fixtures,
and status() is checked against a small HTTP server on 127.0.0.1.
"""
import contextlib
import http.client
import http.server
import io
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_playlist as bp  # noqa: E402


def setUpModule():
    # Never route test requests to 127.0.0.1 through a proxy from the environment
    # or the system settings: an opener without proxies is used for the whole module.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def tearDownModule():
    urllib.request.install_opener(None)


# ---------------------------------------------------------------------------
# entries()
# ---------------------------------------------------------------------------

class EntriesTest(unittest.TestCase):

    def test_extinf_options_and_url(self):
        text = ('#EXTM3U\n'
                '#EXTINF:-1 tvg-id="A.ru" group-title="News",A\n'
                '#EXTVLCOPT:http-user-agent=Agent/1.0\n'
                '#EXTVLCOPT:http-referrer=https://ref.example/\n'
                'http://a.example/live.m3u8\n'
                '#EXTINF:-1 group-title="Kids",B\n'
                'https://b.example/b.m3u8\n')
        self.assertEqual(bp.entries(text), [
            ('#EXTINF:-1 tvg-id="A.ru" group-title="News",A',
             ['#EXTVLCOPT:http-user-agent=Agent/1.0', '#EXTVLCOPT:http-referrer=https://ref.example/'],
             'http://a.example/live.m3u8'),
            ('#EXTINF:-1 group-title="Kids",B', [], 'https://b.example/b.m3u8'),
        ])

    def test_lines_without_extinf_are_ignored(self):
        text = ('#EXTM3U\n'
                'http://orphan.example/first.m3u8\n'
                '#EXTVLCOPT:http-user-agent=Lost/1.0\n'
                '#EXTINF:-1,A\n'
                'http://a.example/a.m3u8\n'
                'http://orphan.example/second.m3u8\n')
        self.assertEqual(bp.entries(text), [('#EXTINF:-1,A', [], 'http://a.example/a.m3u8')])

    def test_blank_lines_and_whitespace(self):
        text = ('\n#EXTM3U\n\n'
                '  #EXTINF:-1,A  \n'
                '\n'
                '   \n'
                '\t#EXTVLCOPT:http-referrer=https://ref.example/\n'
                '\n'
                '  http://a.example/a.m3u8  \n'
                '\n\n')
        self.assertEqual(bp.entries(text), [
            ('#EXTINF:-1,A', ['#EXTVLCOPT:http-referrer=https://ref.example/'], 'http://a.example/a.m3u8'),
        ])

    def test_crlf_line_endings(self):
        # The iptv-org lists are served with CRLF line endings.
        text = '#EXTM3U\r\n#EXTINF:-1,A\r\n#EXTVLCOPT:http-user-agent=X\r\nhttp://a.example/a.m3u8\r\n'
        self.assertEqual(bp.entries(text), [('#EXTINF:-1,A', ['#EXTVLCOPT:http-user-agent=X'],
                                             'http://a.example/a.m3u8')])

    def test_extinf_without_url_is_dropped(self):
        text = ('#EXTINF:-1,No URL\n'
                '#EXTVLCOPT:http-user-agent=Old\n'
                '#EXTINF:-1,B\n'
                'http://b.example/b.m3u8\n'
                '#EXTINF:-1,Last without URL\n')
        # Options of the dropped entry must not leak into the next one.
        self.assertEqual(bp.entries(text), [('#EXTINF:-1,B', [], 'http://b.example/b.m3u8')])

    def test_other_tags_are_not_options(self):
        text = ('#EXTINF:-1,A\n'
                '#EXTGRP:Films\n'
                '#KODIPROP:inputstream=inputstream.adaptive\n'
                'http://a.example/a.m3u8\n')
        self.assertEqual(bp.entries(text), [('#EXTINF:-1,A', [], 'http://a.example/a.m3u8')])

    def test_empty_input(self):
        self.assertEqual(bp.entries(''), [])
        self.assertEqual(bp.entries('#EXTM3U\n'), [])


# ---------------------------------------------------------------------------
# host()
# ---------------------------------------------------------------------------

class HostTest(unittest.TestCase):

    def test_hosts(self):
        cases = {
            'http://example.com/live/index.m3u8': 'example.com',
            'https://example.com/live/index.m3u8': 'example.com',
            'http://example.com': 'example.com',
            'http://example.com:8080/live.m3u8': 'example.com',
            'https://10.0.0.1:443/live.m3u8': '10.0.0.1',
            'http://example.com?token=abc': 'example.com',
            'https://example.com/index.m3u8?redirect=http://other.example/': 'example.com',
            'http://example.com#fragment': 'example.com',
            'HTTPS://JMP2.UK/plu-5ca525b650be2571e3943c63.m3u8': 'jmp2.uk',
            'Http://Sub.Example.COM:8000/Path?Q=1': 'sub.example.com',
            'rtmp://89.28.25.122/live/tnt_md': '89.28.25.122',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(bp.host(url), expected)

    def test_excluded_host_matches_any_case_and_port(self):
        for url in ('https://jmp2.uk/plu-1.m3u8', 'http://JMP2.uk:80/sam-2.m3u8', 'https://jmp2.uk?x=1'):
            with self.subTest(url=url):
                self.assertIn(bp.host(url), bp.EXCLUDED_HOSTS)
        self.assertNotIn(bp.host('https://notjmp2.uk/a.m3u8'), bp.EXCLUDED_HOSTS)


# ---------------------------------------------------------------------------
# status() against a real HTTP server on 127.0.0.1
# ---------------------------------------------------------------------------

HLS = b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n'
SIMPLE_M3U = b'#EXTM3U\n#EXTINF:-1,Channel\nhttp://example.com/stream\n'
TS = bytes([0x47, 0x40, 0x00, 0x10]) + bytes(184) + bytes([0x47]) * 188


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False   # do not wait for hanging handlers on shutdown


class _Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _send(self, code, body=b'', ctype='application/octet-stream', extra=()):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, data):
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        self.server.requests.append((path, self.headers))   # case-insensitive lookups
        mpegurl = 'application/vnd.apple.mpegurl'
        if path == '/hls.m3u8':
            self._send(200, HLS, mpegurl)
        elif path == '/simple.m3u':
            self._send(200, SIMPLE_M3U, 'audio/x-mpegurl')
        elif path == '/hls-as-html':
            self._send(200, HLS, 'text/html')        # misconfigured server, content decides
        elif path == '/m3u-header-only':
            self._send(200, b'#EXTM3U\n', mpegurl)
        elif path == '/html':
            self._send(200, b'<!DOCTYPE html><html><body>Channel is offline</body></html>',
                       'text/html; charset=utf-8')
        elif path == '/html-upper':
            # Media types are case-insensitive (RFC 9110, 8.3.1).
            self._send(200, b'<HTML><BODY>Access denied</BODY></HTML>', 'Text/HTML; Charset=UTF-8')
        elif path == '/ts':
            self._send(200, TS, 'video/mp2t')
        elif path == '/empty':
            self._send(200, b'', 'video/mp2t')
        elif path == '/no-content':
            self.send_response(204)
            self.end_headers()
        elif path.startswith('/code/'):
            self._send(int(path.rsplit('/', 1)[1]), b'error page', 'text/html')
        elif path == '/redirect':
            self._send(302, b'', extra=[('Location', '/hls.m3u8')])
        elif path == '/redirect-to-echo':
            self._send(302, b'', extra=[('Location', '/echo')])
        elif path == '/echo':
            self._send(200, HLS, mpegurl)
        elif path == '/hang-before-headers':
            self.server.release.wait(10)
        elif path == '/hang-after-headers':
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Content-Length', '100000')
            self.end_headers()
            self.wfile.flush()
            self.server.release.wait(10)
        elif path == '/chunked-short':
            # The chunk announces 0x800 bytes, the server sends a valid playlist head and hangs up.
            self._raw(b'HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n'
                      b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n'
                      b'800\r\n' + HLS)
        elif path == '/chunked-no-end':
            # A complete first chunk, then the connection drops before the terminating chunk.
            self._raw(b'HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n'
                      b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n'
                      + b'%x\r\n' % len(HLS) + HLS + b'\r\n')
        elif path == '/chunked-garbage':
            self._raw(b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n'
                      b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n'
                      b'zz-not-a-size\r\n' + TS)
        else:
            self._send(404, b'not found', 'text/plain')


def _short_timeout(seconds):
    """A urlopen replacement with a short timeout, so hanging servers do not slow the tests down."""
    real = urllib.request.urlopen

    def urlopen(url, data=None, timeout=None, **kw):
        return real(url, data, timeout=seconds, **kw)
    return urlopen


class StatusServerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(('127.0.0.1', 0), _Handler)
        cls.server.requests = []
        cls.server.release = threading.Event()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.server.requests.clear()

    def check(self, path, opts=()):
        return bp.status(('#EXTINF:-1,Test', list(opts), self.base + path))

    def assertStatus(self, path, expected, opts=()):
        self.assertEqual(self.check(path, opts), expected, path)

    # 200 answers

    def test_hls_playlist_is_ok(self):
        self.assertStatus('/hls.m3u8', 'ok')

    def test_plain_m3u_with_extinf_is_ok(self):
        self.assertStatus('/simple.m3u', 'ok')

    def test_playlist_content_wins_over_html_content_type(self):
        self.assertStatus('/hls-as-html', 'ok')

    def test_extm3u_without_media_tags_is_dead(self):
        self.assertStatus('/m3u-header-only', 'dead')

    def test_html_page_is_dead(self):
        self.assertStatus('/html', 'dead')

    def test_html_content_type_in_upper_case_is_dead(self):
        self.assertStatus('/html-upper', 'dead')

    def test_binary_stream_is_ok(self):
        self.assertStatus('/ts', 'ok')

    def test_empty_body_is_dead(self):
        self.assertStatus('/empty', 'dead')

    def test_non_200_success_is_dead(self):
        self.assertStatus('/no-content', 'dead')

    def test_redirect_is_followed(self):
        self.assertStatus('/redirect', 'ok')

    # HTTP errors

    def test_geo_codes(self):
        for code in (403, 451, 400, 401):
            with self.subTest(code=code):
                self.assertStatus('/code/%d' % code, 'geo')

    def test_dead_codes(self):
        for code in (404, 410, 500, 502, 503):
            with self.subTest(code=code):
                self.assertStatus('/code/%d' % code, 'dead')

    # Broken connections

    def test_timeout_before_headers_is_dead(self):
        with mock.patch('urllib.request.urlopen', _short_timeout(0.3)):
            self.assertStatus('/hang-before-headers', 'dead')

    def test_timeout_while_reading_body_is_dead(self):
        with mock.patch('urllib.request.urlopen', _short_timeout(0.3)):
            self.assertStatus('/hang-after-headers', 'dead')

    def test_broken_chunked_responses_raise_incomplete_read(self):
        # Make sure the fixtures really produce http.client.IncompleteRead,
        # so the next test checks what it claims to check.
        for path in ('/chunked-short', '/chunked-no-end', '/chunked-garbage'):
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=5) as r:
                    with self.assertRaises(http.client.IncompleteRead):
                        r.read(2048)

    def test_broken_chunked_response_is_dead(self):
        for path in ('/chunked-short', '/chunked-no-end', '/chunked-garbage'):
            with self.subTest(path=path):
                self.assertStatus(path, 'dead')

    def test_connection_refused_is_dead(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        self.assertEqual(bp.status(('#EXTINF:-1,X', [], 'http://127.0.0.1:%d/a.m3u8' % port)), 'dead')

    def test_unsupported_or_bad_urls_are_dead(self):
        # rtmp://, mmsh:// and srt:// links do appear in the iptv-org lists.
        for url in ('rtmp://127.0.0.1/live/x', 'mmsh://127.0.0.1/x', 'srt://127.0.0.1:4001',
                    'not a url', 'http://'):
            with self.subTest(url=url):
                self.assertEqual(bp.status(('#EXTINF:-1,X', [], url)), 'dead')

    # Request headers

    def last_headers(self, path):
        found = [h for p, h in self.server.requests if p == path]
        self.assertTrue(found, 'no request reached %s' % path)
        return found[-1]

    def test_default_user_agent(self):
        self.assertStatus('/echo', 'ok')
        headers = self.last_headers('/echo')
        self.assertEqual(headers.get('User-Agent'), bp.UA)
        self.assertNotIn('Referer', headers)

    def test_extvlcopt_user_agent_and_referrer_are_sent(self):
        opts = ['#EXTVLCOPT:http-user-agent=TestAgent/2.0 (KHTML, like Gecko)',
                '#EXTVLCOPT:http-referrer=https://ref.example/page?x=1',
                '#EXTVLCOPT:http-origin=https://origin.example']
        self.assertStatus('/echo', 'ok', opts)
        headers = self.last_headers('/echo')
        self.assertEqual(headers.get('User-Agent'), 'TestAgent/2.0 (KHTML, like Gecko)')
        self.assertEqual(headers.get('Referer'), 'https://ref.example/page?x=1')
        self.assertNotIn('Origin', headers)

    def test_headers_survive_redirects(self):
        opts = ['#EXTVLCOPT:http-user-agent=Redirected/1.0', '#EXTVLCOPT:http-referrer=https://ref.example/']
        self.assertStatus('/redirect-to-echo', 'ok', opts)
        headers = self.last_headers('/echo')
        self.assertEqual(headers.get('User-Agent'), 'Redirected/1.0')
        self.assertEqual(headers.get('Referer'), 'https://ref.example/')


# ---------------------------------------------------------------------------
# status() with urlopen mocked (exceptions that are awkward to provoke for real)
# ---------------------------------------------------------------------------

class _FakeResponse:

    def __init__(self, status=200, body=b'', ctype='', read_error=None):
        self.status, self.body, self.read_error = status, body, read_error
        self.headers = {'Content-Type': ctype} if ctype else {}

    def read(self, amt=None):
        if self.read_error:
            raise self.read_error
        return self.body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StatusMockTest(unittest.TestCase):
    ITEM = ('#EXTINF:-1,X', [], 'http://stream.example/live.m3u8')

    def run_status(self, result=None, error=None):
        with mock.patch('urllib.request.urlopen', return_value=result, side_effect=error) as urlopen:
            return bp.status(self.ITEM), urlopen

    def test_ok_and_dead_bodies(self):
        cases = [
            (_FakeResponse(200, HLS, 'application/vnd.apple.mpegurl'), 'ok'),
            (_FakeResponse(200, b'\xef\xbb\xbf' + HLS, 'application/x-mpegURL'), 'ok'),   # UTF-8 BOM
            (_FakeResponse(200, b'<html></html>', 'text/html'), 'dead'),
            (_FakeResponse(200, TS, ''), 'ok'),
        ]
        for response, expected in cases:
            with self.subTest(body=response.body[:20], status=response.status):
                self.assertEqual(self.run_status(result=response)[0], expected)

    def test_timeouts_are_dead(self):
        for error in (socket.timeout('timed out'), TimeoutError('timed out'),
                      urllib.error.URLError(socket.timeout('timed out'))):
            with self.subTest(error=repr(error)):
                self.assertEqual(self.run_status(error=error)[0], 'dead')

    def test_timeout_while_reading_is_dead(self):
        response = _FakeResponse(200, read_error=socket.timeout('timed out'))
        self.assertEqual(self.run_status(result=response)[0], 'dead')

    def test_incomplete_read_is_dead(self):
        response = _FakeResponse(200, read_error=http.client.IncompleteRead(HLS[:10], 100))
        self.assertEqual(self.run_status(result=response)[0], 'dead')

    def test_http_errors(self):
        for code, expected in ((403, 'geo'), (451, 'geo'), (400, 'geo'), (401, 'geo'),
                               (404, 'dead'), (500, 'dead')):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(self.ITEM[2], code, 'error', {}, io.BytesIO())
                self.assertEqual(self.run_status(error=error)[0], expected)

    def test_request_headers_and_timeout(self):
        item = ('#EXTINF:-1,X', ['#EXTVLCOPT:http-user-agent=Agent/3', '#EXTVLCOPT:http-referrer=https://r.example/'],
                'http://stream.example/live.m3u8')
        with mock.patch('urllib.request.urlopen', return_value=_FakeResponse(200, HLS)) as urlopen:
            self.assertEqual(bp.status(item), 'ok')
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, item[2])
        self.assertEqual(req.get_header('User-agent'), 'Agent/3')
        self.assertEqual(req.get_header('Referer'), 'https://r.example/')
        timeout = urlopen.call_args.kwargs.get('timeout')
        self.assertIsNotNone(timeout, 'status() must always pass a timeout')
        self.assertGreater(timeout, 0)


# ---------------------------------------------------------------------------
# check_all()
# ---------------------------------------------------------------------------

class CheckAllTest(unittest.TestCase):

    def test_failed_streams_are_retried_once(self):
        answers = {'http://ok/': ['ok'], 'http://flaky/': ['dead', 'ok'],
                   'http://geo/': ['geo', 'geo'], 'http://dead/': ['dead', 'dead']}
        calls = []
        lock = threading.Lock()

        def fake(item):
            with lock:
                calls.append(item[2])
                return answers[item[2]][calls.count(item[2]) - 1]
        items = {u: (None, [], u) for u in answers}
        with mock.patch.object(bp, 'status', side_effect=fake):
            res = bp.check_all(items)
        self.assertEqual(res, {'http://ok/': 'ok', 'http://flaky/': 'ok', 'http://geo/': 'geo',
                               'http://dead/': 'dead'})
        self.assertEqual(calls.count('http://ok/'), 1)
        for u in ('http://flaky/', 'http://geo/', 'http://dead/'):
            self.assertEqual(calls.count(u), 2, u)

    def test_empty(self):
        with mock.patch.object(bp, 'status') as status:
            self.assertEqual(bp.check_all({}), {})
        status.assert_not_called()


# ---------------------------------------------------------------------------
# read_blocklist()
# ---------------------------------------------------------------------------

class ReadBlocklistTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name='blocklist.txt'):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        return path

    def test_missing_file(self):
        self.assertEqual(bp.read_blocklist(os.path.join(self.tmp.name, 'nope.txt')), set())

    def test_no_path(self):
        self.assertEqual(bp.read_blocklist(None), set())
        self.assertEqual(bp.read_blocklist(''), set())

    def test_comments_and_blank_lines(self):
        path = self.write('# Streams that did not answer from Russia; CI builds skip them.\n'
                          'http://a.example/a.m3u8\n'
                          '\n'
                          '   \n'
                          '# another comment\n'
                          '  http://b.example/b.m3u8  \r\n'
                          'http://a.example/a.m3u8\n'
                          'http://c.example/c.m3u8')        # no trailing newline
        self.assertEqual(bp.read_blocklist(path),
                         {'http://a.example/a.m3u8', 'http://b.example/b.m3u8', 'http://c.example/c.m3u8'})

    def test_only_comments(self):
        self.assertEqual(bp.read_blocklist(self.write('# header\n\n')), set())
        self.assertEqual(bp.read_blocklist(self.write('')), set())

    def test_repository_blocklist_is_readable(self):
        path = os.path.join(ROOT, 'ru-blocklist.txt')
        if not os.path.exists(path):
            self.skipTest('ru-blocklist.txt not present')
        urls = bp.read_blocklist(path)
        self.assertTrue(urls)
        for u in urls:
            self.assertFalse(u.startswith('#'), u)
            self.assertEqual(u, u.strip())


# ---------------------------------------------------------------------------
# main() end to end, with fetch() and status() mocked
# ---------------------------------------------------------------------------

def m3u(*entries):
    """Builds a source list the way iptv-org serves it (CRLF line endings)."""
    lines = ['#EXTM3U']
    for e in entries:
        lines.extend(e)
    return '\r\n'.join(lines) + '\r\n'


class MainTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, 'out.m3u')
        self.blocklist = os.path.join(self.tmp.name, 'blocklist.txt')

    def run_main(self, lists, statuses=None, args=(), blocked=None):
        """Runs main() with the given source lists ({path: text}) and statuses ({url: status}).

        Returns (playlist text, stdout, list of URLs passed to status())."""
        statuses = statuses or {}
        if blocked is not None:
            with open(self.blocklist, 'w', encoding='utf-8') as f:
                f.write('# test blocklist\n' + '\n'.join(blocked) + '\n')
        unknown = set(lists) - {path for _, path, _ in bp.SOURCES}
        self.assertFalse(unknown, 'fixture paths not in SOURCES')
        checked = []

        def fake_status(item):
            checked.append(item)
            return statuses.get(item[2], 'ok')

        argv = ['build_playlist.py', self.out, '--blocklist', self.blocklist] + list(args)
        stdout = io.StringIO()
        with mock.patch.object(bp, 'fetch', side_effect=lambda path: lists.get(path, '#EXTM3U\r\n')) as fetch, \
                mock.patch.object(bp, 'status', side_effect=fake_status), \
                mock.patch.object(sys, 'argv', argv), \
                contextlib.redirect_stdout(stdout):
            bp.main()
        self.assertEqual(sorted(c.args[0] for c in fetch.call_args_list), sorted(p for _, p, _ in bp.SOURCES))
        with open(self.out, 'rb') as f:
            playlist = f.read().decode('utf-8')
        self.checked_items = checked
        return playlist, stdout.getvalue(), sorted({item[2] for item in checked})

    def test_group_title_is_rewritten(self):
        lists = {'languages/rus.m3u': m3u(
            ['#EXTINF:-1 tvg-id="One.ru@SD" tvg-logo="https://i.example/1.png" group-title="General",Первый (576p)',
             'http://one.example/live.m3u8'],
            ['#EXTINF:-1 tvg-id="Two.ru@SD" group-title="News;Movies",Two',
             'http://two.example/live.m3u8'],
            ['#EXTINF:-1 tvg-id="Three.ru@SD" group-title="",Three',
             'http://three.example/live.m3u8'],
        )}
        playlist, _, _ = self.run_main(lists)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 tvg-id="One.ru@SD" tvg-logo="https://i.example/1.png" group-title="Русские",Первый (576p)\n'
                         'http://one.example/live.m3u8\n'
                         '#EXTINF:-1 tvg-id="Two.ru@SD" group-title="Русские",Two\n'
                         'http://two.example/live.m3u8\n'
                         '#EXTINF:-1 tvg-id="Three.ru@SD" group-title="Русские",Three\n'
                         'http://three.example/live.m3u8\n')

    def test_group_title_is_added_when_missing(self):
        lists = {'categories/music.m3u': m3u(
            ['#EXTINF:-1 tvg-id="A.rs",A', 'http://a.example/a.m3u8'],
            ['#EXTINF:-1,B', 'http://b.example/b.m3u8'],
        )}
        playlist, _, _ = self.run_main(lists)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Музыка" tvg-id="A.rs",A\n'
                         'http://a.example/a.m3u8\n'
                         '#EXTINF:-1 group-title="Музыка",B\n'
                         'http://b.example/b.m3u8\n')

    def test_options_are_kept_and_used_for_the_check(self):
        lists = {'languages/rus.m3u': m3u(
            ['#EXTINF:-1 tvg-id="Ref.ru" http-referrer="https://ref.example/" group-title="General",Ref',
             '#EXTVLCOPT:http-user-agent=Agent/1.0',
             '#EXTVLCOPT:http-referrer=https://ref.example/',
             'http://ref.example/live.m3u8'],
        )}
        playlist, _, _ = self.run_main(lists)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 tvg-id="Ref.ru" http-referrer="https://ref.example/" group-title="Русские",Ref\n'
                         '#EXTVLCOPT:http-user-agent=Agent/1.0\n'
                         '#EXTVLCOPT:http-referrer=https://ref.example/\n'
                         'http://ref.example/live.m3u8\n')
        self.assertEqual([item[1] for item in self.checked_items],
                         [['#EXTVLCOPT:http-user-agent=Agent/1.0', '#EXTVLCOPT:http-referrer=https://ref.example/']])

    def test_duplicates_inside_a_group_are_dropped(self):
        same = 'http://serbia.example/rts1.m3u8'
        lists = {
            # The same stream in two lists of the same output group, and twice in one list.
            'countries/rs.m3u': m3u(['#EXTINF:-1 group-title="General",RTS 1', same],
                                    ['#EXTINF:-1 group-title="General",RTS 1 copy', same]),
            'languages/srp.m3u': m3u(['#EXTINF:-1 group-title="News",RTS 1 (srp)', same],
                                     ['#EXTINF:-1 group-title="News",RTS 2', 'http://serbia.example/rts2.m3u8']),
            # The same stream in another output group is kept there as well.
            'categories/music.m3u': m3u(['#EXTINF:-1 group-title="Music",RTS 1 music', same]),
        }
        playlist, out, checked = self.run_main(lists)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Сербия и Босния",RTS 1\n'
                         + same + '\n'
                         '#EXTINF:-1 group-title="Сербия и Босния",RTS 2\n'
                         'http://serbia.example/rts2.m3u8\n'
                         '#EXTINF:-1 group-title="Музыка",RTS 1 music\n'
                         + same + '\n')
        # Each unique URL is checked once.
        self.assertEqual(len(self.checked_items), 2)
        self.assertEqual(checked, [same, 'http://serbia.example/rts2.m3u8'])
        self.assertIn('checking 2 unique streams', out)
        self.assertIn('  Сербия и Босния: 2\n', out)
        self.assertIn('  Музыка: 1\n', out)

    def test_english_list_is_filtered_by_source_group(self):
        lists = {'languages/eng.m3u': m3u(
            ['#EXTINF:-1 group-title="Kids",Kids', 'http://en.example/kids.m3u8'],
            ['#EXTINF:-1 group-title="Animation;Education",Edu', 'http://en.example/edu.m3u8'],
            ['#EXTINF:-1 group-title="documentary",Doc', 'http://en.example/doc.m3u8'],
            ['#EXTINF:-1 group-title="News",News', 'http://en.example/news.m3u8'],
            ['#EXTINF:-1 tvg-id="NoGroup.us",No group', 'http://en.example/nogroup.m3u8'],
        )}
        playlist, _, checked = self.run_main(lists)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="English (для изучения)",Kids\n'
                         'http://en.example/kids.m3u8\n'
                         '#EXTINF:-1 group-title="English (для изучения)",Edu\n'
                         'http://en.example/edu.m3u8\n'
                         '#EXTINF:-1 group-title="English (для изучения)",Doc\n'
                         'http://en.example/doc.m3u8\n')
        self.assertNotIn('http://en.example/news.m3u8', checked)
        self.assertNotIn('http://en.example/nogroup.m3u8', checked)

    def test_excluded_hosts_and_blocklist_are_skipped(self):
        lists = {'categories/kids.m3u': m3u(
            ['#EXTINF:-1 group-title="Kids",Pluto', 'https://jmp2.uk/plu-5ca525b650be2571e3943c63.m3u8'],
            ['#EXTINF:-1 group-title="Kids",Samsung', 'HTTPS://JMP2.UK:443/sam-GBBD8000016N.m3u8'],
            ['#EXTINF:-1 group-title="Kids",Blocked', 'http://blocked.example/a.m3u8'],
            ['#EXTINF:-1 group-title="Kids",Fine', 'http://fine.example/a.m3u8'],
        )}
        playlist, _, checked = self.run_main(lists, blocked=['http://blocked.example/a.m3u8'])
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Детские",Fine\n'
                         'http://fine.example/a.m3u8\n')
        self.assertEqual(checked, ['http://fine.example/a.m3u8'])

    def test_strict_mode_ignores_blocklist_but_not_excluded_hosts(self):
        lists = {'categories/kids.m3u': m3u(
            ['#EXTINF:-1 group-title="Kids",Pluto', 'https://jmp2.uk/plu-1.m3u8'],
            ['#EXTINF:-1 group-title="Kids",Blocked', 'http://blocked.example/a.m3u8'],
        )}
        playlist, _, checked = self.run_main(lists, args=['--strict'], blocked=['http://blocked.example/a.m3u8'])
        self.assertEqual(checked, ['http://blocked.example/a.m3u8'])
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Детские",Blocked\n'
                         'http://blocked.example/a.m3u8\n')

    STATUS_LISTS = {'categories/education.m3u': m3u(
        ['#EXTINF:-1 group-title="Education",Ok', 'http://ok.example/a.m3u8'],
        ['#EXTINF:-1 group-title="Education",Geo', 'http://geo.example/a.m3u8'],
        ['#EXTINF:-1 group-title="Education",Dead', 'http://dead.example/a.m3u8'],
        ['#EXTINF:-1 group-title="Education",Geo 2', 'http://geo2.example/a.m3u8'],
    )}
    STATUSES = {'http://ok.example/a.m3u8': 'ok', 'http://geo.example/a.m3u8': 'geo',
                'http://dead.example/a.m3u8': 'dead', 'http://geo2.example/a.m3u8': 'geo'}

    def test_default_mode_keeps_ok_and_geo(self):
        playlist, out, _ = self.run_main(self.STATUS_LISTS, self.STATUSES)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Обучающие",Ok\n'
                         'http://ok.example/a.m3u8\n'
                         '#EXTINF:-1 group-title="Обучающие",Geo\n'
                         'http://geo.example/a.m3u8\n'
                         '#EXTINF:-1 group-title="Обучающие",Geo 2\n'
                         'http://geo2.example/a.m3u8\n')
        self.assertIn('ok 1, geo 2, dead 1 of 4 unique', out)
        self.assertIn('  Обучающие: 3\n', out)

    def test_strict_mode_keeps_only_ok(self):
        playlist, out, _ = self.run_main(self.STATUS_LISTS, self.STATUSES, args=['--strict'])
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Обучающие",Ok\n'
                         'http://ok.example/a.m3u8\n')
        self.assertIn('  Обучающие: 1\n', out)

    def test_write_blocklist(self):
        path = os.path.join(self.tmp.name, 'new-blocklist.txt')
        _, out, _ = self.run_main(self.STATUS_LISTS, self.STATUSES, args=['--strict', '--write-blocklist', path])
        with open(path, 'rb') as f:
            content = f.read().decode('utf-8')
        self.assertEqual(content,
                         '# Streams that did not answer from Russia; CI builds skip them.\n'
                         'http://dead.example/a.m3u8\n'
                         'http://geo.example/a.m3u8\n'
                         'http://geo2.example/a.m3u8\n')
        self.assertIn('blocklist: 3 URLs -> %s' % path, out)
        # What the strict run writes is exactly what the next CI run skips.
        self.assertEqual(bp.read_blocklist(path),
                         {'http://dead.example/a.m3u8', 'http://geo.example/a.m3u8', 'http://geo2.example/a.m3u8'})
        _, _, checked = self.run_main(self.STATUS_LISTS, self.STATUSES, args=['--blocklist', path])
        self.assertEqual(checked, ['http://ok.example/a.m3u8'])

    def test_write_blocklist_needs_strict(self):
        path = os.path.join(self.tmp.name, 'new-blocklist.txt')
        self.run_main(self.STATUS_LISTS, self.STATUSES, args=['--write-blocklist', path])
        self.assertFalse(os.path.exists(path))

    def test_groups_follow_sources_order(self):
        lists = {
            'categories/music.m3u': m3u(['#EXTINF:-1 group-title="Music",M', 'http://m.example/a.m3u8']),
            'languages/rus.m3u': m3u(['#EXTINF:-1 group-title="General",R', 'http://r.example/a.m3u8']),
            'languages/bos.m3u': m3u(['#EXTINF:-1 group-title="General",B', 'http://b.example/a.m3u8']),
        }
        playlist, out, _ = self.run_main(lists)
        self.assertEqual(playlist,
                         '#EXTM3U\n'
                         '#EXTINF:-1 group-title="Русские",R\n'
                         'http://r.example/a.m3u8\n'
                         '#EXTINF:-1 group-title="Сербия и Босния",B\n'
                         'http://b.example/a.m3u8\n'
                         '#EXTINF:-1 group-title="Музыка",M\n'
                         'http://m.example/a.m3u8\n')
        self.assertLess(out.index('Русские: 1'), out.index('Сербия и Босния: 1'))
        self.assertLess(out.index('Сербия и Босния: 1'), out.index('Музыка: 1'))


if __name__ == '__main__':
    unittest.main()
