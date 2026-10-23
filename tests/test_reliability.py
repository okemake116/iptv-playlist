"""Tests for the grace period, the shrink safety net, sorting and the EPG header (no internet)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_playlist as bp  # noqa: E402

KEEP = {'ok', 'geo'}


def m3u(*channels):
    """Source list text from (name, url) or (name, url, source group-title) tuples."""
    lines = ['#EXTM3U']
    for ch in channels:
        name, url = ch[0], ch[1]
        group = ch[2] if len(ch) > 2 else 'General'
        lines.append('#EXTINF:-1 tvg-id="%s.ru@SD" group-title="%s",%s' % (name.replace(' ', ''), group, name))
        lines.append(url)
    return '\n'.join(lines) + '\n'


def url(n):
    return 'http://stream.test/%s.m3u8' % n


FILLER = [('~filler %d' % i, 'http://filler.test/%d.m3u8' % i) for i in range(20)]


def is_filler(u):
    return u.startswith('http://filler.test/')


class Runner:
    """Runs main() in a temporary directory with fake iptv-org lists and fake stream checks.

    With pad=True every run also gets 20 always working channels, so that a test about one or two
    streams does not trip the shrink safety net; urls(), names() and fails() leave them out.
    """

    def __init__(self, tmp, pad=True):
        self.out = os.path.join(tmp, 'free-channels.m3u')
        self.state = os.path.join(tmp, 'state', 'fails.json')
        self.blocklist = os.path.join(tmp, 'ru-blocklist.txt')
        self.pad = pad

    def run(self, lists, statuses, *extra, sources=None):
        sources = sources or [('Русские', 'rus.m3u', None)]
        lists, statuses = dict(lists), dict(statuses)
        if self.pad:
            lists[sources[0][1]] += m3u(*FILLER)
            statuses.update((u, 'ok') for _, u in FILLER)
        out = io.StringIO()
        with mock.patch.object(bp, 'SOURCES', sources), \
                mock.patch.object(bp, 'fetch', lambda path: lists[path]), \
                mock.patch.object(bp, 'check_all', lambda items: {u: statuses[u] for u in items}), \
                mock.patch.dict(os.environ, {'GITHUB_ACTIONS': ''}), \
                contextlib.redirect_stdout(out):
            bp.main([self.out, '--blocklist', self.blocklist, *extra])
        return out.getvalue()

    def urls(self):
        with open(self.out, encoding='utf-8') as f:
            return [u for _, _, u in bp.entries(f.read()) if not is_filler(u)]

    def names(self, group=None):
        with open(self.out, encoding='utf-8') as f:
            items = bp.entries(f.read())
        return [bp.channel_name(i) for i, _, u in items
                if not is_filler(u) and (group is None or 'group-title="%s"' % group in i)]

    def fails(self):
        with open(self.state, encoding='utf-8') as f:
            return {u: n for u, n in json.load(f).items() if not is_filler(u)}


class UpdateFailsTest(unittest.TestCase):
    def test_answering_stream_is_reset_to_zero(self):
        self.assertEqual(bp.update_fails({'a': 'ok', 'b': 'geo'}, KEEP, {'a': 2, 'b': 1}, set()),
                         {'a': 0, 'b': 0})

    def test_known_failing_stream_counts_up_until_dropped(self):
        state = {'a': 0}
        seen = []
        for _ in range(4):
            state = bp.update_fails({'a': 'dead'}, KEEP, state, set())
            seen.append(state.get('a'))
        self.assertEqual(seen, [1, 2, None, None])

    def test_never_seen_failing_stream_is_not_added(self):
        self.assertEqual(bp.update_fails({'new': 'dead'}, KEEP, {}, set()), {})

    def test_stream_from_previous_playlist_counts_as_seen(self):
        # First run after this change: there is no state file yet, only the old playlist.
        self.assertEqual(bp.update_fails({'a': 'dead', 'b': 'dead'}, KEEP, {}, {'a'}), {'a': 1})

    def test_urls_gone_from_sources_are_pruned(self):
        self.assertEqual(bp.update_fails({'a': 'ok'}, KEEP, {'a': 0, 'gone': 1}, {'gone'}), {'a': 0})

    def test_geo_fails_in_strict_keep_set(self):
        self.assertEqual(bp.update_fails({'a': 'geo'}, {'ok'}, {'a': 0}, set()), {'a': 1})

    def test_max_fails_is_three(self):
        self.assertEqual(bp.MAX_FAILS, 3)


class ShrinkTest(unittest.TestCase):
    def test_threshold(self):
        self.assertFalse(bp.too_small(0, 0))          # no old playlist: nothing to compare
        self.assertFalse(bp.too_small(10, 7))         # exactly 30% fewer is allowed
        self.assertTrue(bp.too_small(10, 6))
        self.assertTrue(bp.too_small(100, 0))
        self.assertFalse(bp.too_small(10, 15))
        self.assertFalse(bp.too_small(10, 0, max_shrink=1))


class ChannelNameTest(unittest.TestCase):
    def test_comma_inside_quoted_attribute(self):
        info = ('#EXTINF:-1 tvg-id="X.ru@SD" http-user-agent="Mozilla/5.0 (KHTML, like Gecko)" '
                'group-title="Русские",Kino 1 (Ukraine), HD')
        self.assertEqual(bp.channel_name(info), 'Kino 1 (Ukraine), HD')

    def test_plain(self):
        self.assertEqual(bp.channel_name('#EXTINF:-1,Первый канал'), 'Первый канал')
        self.assertEqual(bp.channel_name('#EXTINF:-1 tvg-id="x"'), '')


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.sources = mock.patch.object(bp, 'SOURCES', [('A', 'a.m3u', None), ('B', 'b.m3u', None)])
        self.sources.start()
        self.addCleanup(self.sources.stop)

    @staticmethod
    def item(group, name, u):
        return (group, '#EXTINF:-1 group-title="x",%s' % name, [], u)

    def test_sorted_by_name_inside_group_groups_in_sources_order(self):
        groups = [self.item('B', 'Zeta', 'z'), self.item('A', 'ТНТ', '1'), self.item('A', 'bravo', '2'),
                  self.item('A', 'Alpha', '3'), self.item('A', 'Ёлка', '4'), self.item('A', 'абв', '5'),
                  self.item('A', 'Беларусь', '6'), self.item('B', 'alpha', 'y')]
        res = {g[3]: 'ok' for g in groups}
        picked = bp.select(groups, res, set(res), KEEP)
        self.assertEqual([(g, bp.channel_name(i)) for g, i, _, _ in picked],
                         [('A', 'Alpha'), ('A', 'bravo'), ('A', 'абв'), ('A', 'Беларусь'), ('A', 'ТНТ'),
                          ('A', 'Ёлка'), ('B', 'alpha'), ('B', 'Zeta')])

    def test_duplicate_name_keeps_first_working(self):
        groups = [self.item('A', 'Kanal', 'first'), self.item('A', 'KANAL', 'second'),
                  self.item('A', 'Kanal', 'third')]
        res = {'first': 'ok', 'second': 'ok', 'third': 'ok'}
        self.assertEqual([g[3] for g in bp.select(groups, res, set(res), KEEP)], ['first'])

    def test_duplicate_name_prefers_answering_over_grace(self):
        groups = [self.item('A', 'Kanal', 'flaky'), self.item('A', 'Kanal', 'good'),
                  self.item('A', 'Kanal', 'good2')]
        res = {'flaky': 'dead', 'good': 'geo', 'good2': 'ok'}
        self.assertEqual([g[3] for g in bp.select(groups, res, set(res), KEEP)], ['good'])

    def test_duplicate_name_all_in_grace_keeps_first(self):
        groups = [self.item('A', 'Kanal', 'one'), self.item('A', 'Kanal', 'two')]
        res = {'one': 'dead', 'two': 'dead'}
        self.assertEqual([g[3] for g in bp.select(groups, res, {'one', 'two'}, KEEP)], ['one'])

    def test_same_name_in_different_groups_and_same_url_twice(self):
        groups = [self.item('A', 'Kanal', 'u'), self.item('B', 'Kanal', 'u'), self.item('A', 'Other', 'u')]
        picked = bp.select(groups, {'u': 'ok'}, {'u'}, KEEP)
        self.assertEqual([(g, u) for g, _, _, u in picked], [('A', 'u'), ('B', 'u')])

    def test_not_allowed_urls_are_left_out(self):
        groups = [self.item('A', 'One', 'one'), self.item('A', 'Two', 'two')]
        picked = bp.select(groups, {'one': 'ok', 'two': 'dead'}, {'one'}, KEEP)
        self.assertEqual([g[3] for g in picked], ['one'])


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.r = Runner(tmp.name)

    def test_flaky_stream_survives_two_failed_runs(self):
        lists = {'rus.m3u': m3u(('Alpha', url('a')), ('Beta', url('b')))}
        self.r.run(lists, {url('a'): 'ok', url('b'): 'ok'})
        self.assertEqual(self.r.urls(), [url('a'), url('b')])
        self.assertEqual(self.r.fails(), {})

        for run, count in ((1, 1), (2, 2)):
            self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
            self.assertEqual(self.r.urls(), [url('a'), url('b')], 'failed run %d' % run)
            self.assertEqual(self.r.fails(), {url('b'): count})

        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
        self.assertEqual(self.r.urls(), [url('a')])
        self.assertEqual(self.r.fails(), {})

        # Once dropped it is a new stream again: added back only when it answers.
        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
        self.assertEqual(self.r.urls(), [url('a')])
        self.r.run(lists, {url('a'): 'ok', url('b'): 'ok'})
        self.assertEqual(self.r.urls(), [url('a'), url('b')])

    def test_answering_again_resets_count(self):
        lists = {'rus.m3u': m3u(('Alpha', url('a')), ('Beta', url('b')))}
        self.r.run(lists, {url('a'): 'ok', url('b'): 'ok'})
        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
        self.assertEqual(self.r.fails(), {url('b'): 2})
        self.r.run(lists, {url('a'): 'ok', url('b'): 'geo'})     # geo counts as answering in CI
        self.assertEqual(self.r.fails(), {})
        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
        self.assertIn(url('b'), self.r.urls())
        self.assertEqual(self.r.fails(), {url('b'): 1})

    def test_new_failing_stream_not_added_and_gone_stream_pruned(self):
        self.r.run({'rus.m3u': m3u(('Alpha', url('a')), ('Gone', url('g')))},
                   {url('a'): 'ok', url('g'): 'ok'})
        self.r.run({'rus.m3u': m3u(('Alpha', url('a')), ('Gone', url('g')))},
                   {url('a'): 'ok', url('g'): 'dead'})
        self.assertEqual(self.r.fails(), {url('g'): 1})
        out = self.r.run({'rus.m3u': m3u(('Alpha', url('a')), ('New', url('n')))},
                         {url('a'): 'ok', url('n'): 'dead'})
        self.assertEqual(self.r.urls(), [url('a')])
        self.assertEqual(self.r.fails(), {})
        self.assertIn('new failing not added: 1', out)

    def test_first_run_without_state_uses_old_playlist(self):
        with open(self.r.out, 'w', encoding='utf-8') as f:
            f.write(m3u(('Alpha', url('a')), ('Beta', url('b'))))
        self.r.run({'rus.m3u': m3u(('Alpha', url('a')), ('Beta', url('b')), ('Gamma', url('c')))},
                   {url('a'): 'ok', url('b'): 'dead', url('c'): 'dead'})
        self.assertEqual(self.r.urls(), [url('a'), url('b')])
        self.assertEqual(self.r.fails(), {url('b'): 1})

    def test_state_file_lists_only_failing_streams_sorted(self):
        lists = {'rus.m3u': m3u(('Alpha', url('a')), ('Beta', url('b')), ('Gamma', url('c')))}
        self.r.run(lists, {url('a'): 'ok', url('b'): 'ok', url('c'): 'ok'})
        with open(self.r.state, encoding='utf-8') as f:
            self.assertEqual(f.read(), '{}\n')
        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead', url('c'): 'geo'})
        self.r.run(lists, {url('a'): 'dead', url('b'): 'dead', url('c'): 'ok'})
        with open(self.r.state, encoding='utf-8') as f:
            self.assertEqual(f.read(), '{\n"%s": 1,\n"%s": 2\n}\n' % (url('a'), url('b')))

    def test_blocklisted_url_is_skipped_and_pruned(self):
        lists = {'rus.m3u': m3u(('Alpha', url('a')), ('Beta', url('b')))}
        self.r.run(lists, {url('a'): 'ok', url('b'): 'ok'})
        self.r.run(lists, {url('a'): 'ok', url('b'): 'dead'})
        self.assertEqual(self.r.fails(), {url('b'): 1})
        with open(self.r.blocklist, 'w', encoding='utf-8') as f:
            f.write('# comment\n%s\n' % url('b'))
        self.r.run(lists, {url('a'): 'ok'})
        self.assertEqual(self.r.urls(), [url('a')])
        self.assertEqual(self.r.fails(), {})

    def test_header_has_epg_and_groups_are_sorted(self):
        sources = [('Русские', 'rus.m3u', None), ('Музыка', 'music.m3u', None)]
        lists = {'rus.m3u': m3u(('ТНТ', url(1)), ('2x2', url(2)), ('Карусель', url(3)), ('amedia', url(4))),
                 'music.m3u': m3u(('Zvezda', url(5)), ('Bridge', url(6)))}
        self.r.run(lists, {url(i): 'ok' for i in range(1, 7)}, sources=sources)
        with open(self.r.out, encoding='utf-8') as f:
            first = f.readline().rstrip('\n')
        self.assertEqual(first, '#EXTM3U url-tvg="%s"' % bp.EPG_URL)
        self.assertTrue(bp.EPG_URL.startswith('https://'))
        self.assertEqual(self.r.names('Русские'), ['2x2', 'amedia', 'Карусель', 'ТНТ'])
        self.assertEqual(self.r.names('Музыка'), ['Bridge', 'Zvezda'])
        self.assertEqual(self.r.names(), ['2x2', 'amedia', 'Карусель', 'ТНТ', 'Bridge', 'Zvezda'])

    def test_duplicate_name_in_group_keeps_first_working(self):
        lists = {'rus.m3u': m3u(('Kanal', url('x')), ('Kanal', url('y')), ('Kanal', url('z')))}
        self.r.run(lists, {url('x'): 'dead', url('y'): 'ok', url('z'): 'ok'})
        self.assertEqual(self.r.urls(), [url('y')])

    def test_shrink_over_30_percent_keeps_old_files_and_fails(self):
        self.r.pad = False
        ten = [('Ch%d' % i, url(i)) for i in range(10)]
        lists = {'rus.m3u': m3u(*ten)}
        self.r.run(lists, {url(i): 'ok' for i in range(10)})
        with open(self.r.out, encoding='utf-8') as f:
            playlist = f.read()
        with open(self.r.state, encoding='utf-8') as f:
            state = f.read()

        # iptv-org returned only 6 of 10 channels: 40% fewer.
        with self.assertRaises(SystemExit) as cm:
            self.r.run({'rus.m3u': m3u(*ten[:6])}, {url(i): 'ok' for i in range(6)})
        self.assertNotIn(cm.exception.code, (None, 0))
        self.assertIn('kept the old playlist', str(cm.exception.code))
        with open(self.r.out, encoding='utf-8') as f:
            self.assertEqual(f.read(), playlist)
        with open(self.r.state, encoding='utf-8') as f:
            self.assertEqual(f.read(), state)

    def test_shrink_of_30_percent_or_forced_is_written(self):
        self.r.pad = False
        ten = [('Ch%d' % i, url(i)) for i in range(10)]
        self.r.run({'rus.m3u': m3u(*ten)}, {url(i): 'ok' for i in range(10)})
        self.r.run({'rus.m3u': m3u(*ten[:7])}, {url(i): 'ok' for i in range(7)})
        self.assertEqual(len(self.r.urls()), 7)
        self.r.run({'rus.m3u': m3u(*ten[:1])}, {url(0): 'ok'}, '--max-shrink', '1')
        self.assertEqual(self.r.urls(), [url(0)])

    def test_network_down_once_does_not_empty_playlist(self):
        self.r.pad = False
        ten = [('Ch%d' % i, url(i)) for i in range(10)]
        lists = {'rus.m3u': m3u(*ten)}
        self.r.run(lists, {url(i): 'ok' for i in range(10)})
        self.r.run(lists, {url(i): 'dead' for i in range(10)})
        self.assertEqual(len(self.r.urls()), 10)
        self.r.run(lists, {url(i): 'dead' for i in range(10)})
        self.assertEqual(len(self.r.urls()), 10)
        with self.assertRaises(SystemExit):      # third failed run would empty it
            self.r.run(lists, {url(i): 'dead' for i in range(10)})
        self.assertEqual(len(self.r.urls()), 10)

    def test_github_error_annotation(self):
        self.r.pad = False
        self.r.run({'rus.m3u': m3u(*[('Ch%d' % i, url(i)) for i in range(4)])}, {url(i): 'ok' for i in range(4)})
        out = io.StringIO()
        with mock.patch.object(bp, 'SOURCES', [('Русские', 'rus.m3u', None)]), \
                mock.patch.object(bp, 'fetch', lambda path: m3u()), \
                mock.patch.object(bp, 'check_all', lambda items: {}), \
                mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            bp.main([self.r.out, '--blocklist', self.r.blocklist])
        self.assertIn('::error title=Playlist not updated::', out.getvalue())
        self.assertIn('30%25 fewer', out.getvalue())


class StrictModeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.r = Runner(tmp.name, pad=False)
        self.written = os.path.join(tmp.name, 'written-blocklist.txt')

    def test_strict_keeps_only_ok_writes_blocklist_and_ignores_state(self):
        lists = {'rus.m3u': m3u(*[('Ch%d' % i, url(i)) for i in range(5)])}
        self.r.run(lists, {url(i): 'ok' for i in range(5)})
        with open(self.r.state, encoding='utf-8') as f:
            state = f.read()
        with open(self.r.blocklist, 'w', encoding='utf-8') as f:
            f.write('%s\n' % url(4))          # strict mode re-checks blocklisted URLs too

        statuses = {url(0): 'ok', url(1): 'ok', url(2): 'ok', url(3): 'geo', url(4): 'ok'}
        self.r.run(lists, statuses, '--strict', '--write-blocklist', self.written)
        self.assertEqual(self.r.urls(), [url(0), url(1), url(2), url(4)])
        with open(self.written, encoding='utf-8') as f:
            self.assertEqual(f.read(), '# Streams that did not answer from Russia; CI builds skip them.\n'
                                       '%s\n' % url(3))
        with open(self.r.state, encoding='utf-8') as f:
            self.assertEqual(f.read(), state)

    def test_strict_shrink_writes_nothing(self):
        lists = {'rus.m3u': m3u(*[('Ch%d' % i, url(i)) for i in range(10)])}
        self.r.run(lists, {url(i): 'ok' for i in range(10)})
        with self.assertRaises(SystemExit):
            self.r.run(lists, {url(i): 'dead' for i in range(10)}, '--strict', '--write-blocklist', self.written)
        self.assertFalse(os.path.exists(self.written))
        self.assertEqual(len(self.r.urls()), 10)


if __name__ == '__main__':
    unittest.main()
