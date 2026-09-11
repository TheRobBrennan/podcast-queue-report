"""Unit tests for the deterministic logic in podcast_summary.py.

These build a synthetic in-memory SQLite database that mirrors just the
columns get_unplayed_queue() actually reads from Apple's real
MTLibrary.sqlite, then exercise the filtering/window/dedup logic against
known inputs. They do NOT validate that ZUNPLAYEDTAB, ZBACKCATALOG,
ZENTITLEMENTSTATE etc. mean what we think they mean in the real library -
that's a claim about Apple's undocumented schema and can only be checked
against the real Podcasts.app UI (see CLAUDE.md and the
get_unplayed_queue docstring's long history of screenshot-driven
corrections, including one - ZPLAYSTATE=2 meaning "finished" - that
turned out to be flatly wrong: it silently dropped 16 genuinely-unplayed
episodes from real reports until Rob caught it by counting every row in
four full screenshots against their actual database state). What these
tests DO guarantee is that the code correctly implements its own stated
rules - window bounding, entitlement/cross-promo/stuck-asset exclusions,
per-podcast dedup - so a change to the surrounding logic can't silently
break those rules the way the "78 days behind under a 60-day window" and
"ZPLAYSTATE=2 means played" bugs did.

Run with: python3 -m unittest tests.test_podcast_summary -v
"""
import datetime
import sqlite3
import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import podcast_summary as ps


def make_db():
    con = sqlite3.connect(":memory:")
    con.execute("""
        create table ZMTPODCAST (
            Z_PK integer primary key,
            ZTITLE text,
            ZSTORECLEANURL text,
            ZARTWORKTEMPLATEURL text,
            ZSUBSCRIBED integer
        )
    """)
    con.execute("""
        create table ZMTEPISODE (
            Z_PK integer primary key,
            ZPODCAST integer,
            ZTITLE text,
            ZDURATION real,
            ZPUBDATE real,
            ZPLAYHEAD real,
            ZSTORETRACKID integer,
            ZPLAYSTATE integer,
            ZLASTDATEPLAYED real,
            ZENTITLEMENTSTATE integer,
            ZITEMDESCRIPTION text,
            ZASSETURL text,
            ZBYTESIZE integer,
            ZUNPLAYEDTAB integer,
            ZBACKCATALOG integer
        )
    """)
    return con


class QueueFixture:
    """Helper for building podcast/episode rows without hand-writing SQL
    in every test. Dates are plain datetimes; conversion to Apple's
    Core Data epoch happens here.

    unplayedtab defaults to 1 (the normal case: a freshly-arrived episode
    gets the "new episode unplayed" flag set) so tests that aren't
    specifically about ZUNPLAYEDTAB/ZBACKCATALOG don't need to think
    about them."""

    def __init__(self, con):
        self.con = con
        self.cur = con.cursor()
        self._next_pod_pk = 1
        self._next_ep_pk = 1
        self._next_track_id = 1000

    def podcast(self, title, subscribed=True, url=None):
        pk = self._next_pod_pk
        self._next_pod_pk += 1
        self.cur.execute(
            "insert into ZMTPODCAST (Z_PK, ZTITLE, ZSTORECLEANURL, ZARTWORKTEMPLATEURL, ZSUBSCRIBED) "
            "values (?, ?, ?, ?, ?)",
            (pk, title, url or f"https://example.com/{title}", None, 1 if subscribed else 0),
        )
        return pk

    def episode(self, podcast_pk, title, pubdate, duration=1800, playhead=0,
                playstate=0, entitlement=0, last_played=None, description="",
                asset_url="https://example.com/audio.mp3", byte_size=12345,
                unplayedtab=1, backcatalog=0):
        pk = self._next_ep_pk
        self._next_ep_pk += 1
        track_id = self._next_track_id
        self._next_track_id += 1
        self.cur.execute(
            "insert into ZMTEPISODE (Z_PK, ZPODCAST, ZTITLE, ZDURATION, ZPUBDATE, ZPLAYHEAD, "
            "ZSTORETRACKID, ZPLAYSTATE, ZLASTDATEPLAYED, ZENTITLEMENTSTATE, ZITEMDESCRIPTION, "
            "ZASSETURL, ZBYTESIZE, ZUNPLAYEDTAB, ZBACKCATALOG) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pk, podcast_pk, title, duration, ps.dt_to_cd(pubdate), playhead, track_id,
             playstate, ps.dt_to_cd(last_played) if last_played else None,
             entitlement, description, asset_url, byte_size, unplayedtab, backcatalog),
        )
        return pk


class GetUnplayedQueueTests(unittest.TestCase):
    def setUp(self):
        self.con = make_db()
        self.fx = QueueFixture(self.con)
        self.now = datetime.datetime(2026, 9, 10, 12, 0, 0)

    def titles(self, queue):
        return {e["title"] for e in queue}

    def test_window_bounds_fresh_episode(self):
        """A never-started episode just inside the window shows; one just
        outside does not."""
        pod = self.fx.podcast("Show A")
        self.fx.episode(pod, "inside window", self.now - datetime.timedelta(days=20))
        self.fx.episode(pod, "outside window", self.now - datetime.timedelta(days=22))
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertIn("inside window", self.titles(queue))
        self.assertNotIn("outside window", self.titles(queue))

    def test_window_bounds_started_episode_too(self):
        """Regression for the 2026-09-10 bug: a started-but-unfinished
        episode published outside the window must NOT be exempt from it,
        even if it was recently touched. Previously, a barely-played
        episode published 25 days ago with a last_played 2 days ago would
        surface as '25 days behind' under a 21-day window."""
        pod = self.fx.podcast("Show B")
        self.fx.episode(
            pod, "old but recently touched", self.now - datetime.timedelta(days=25),
            playhead=10, playstate=1, last_played=self.now - datetime.timedelta(days=2),
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertNotIn("old but recently touched", self.titles(queue))

    def test_no_entry_exceeds_the_window(self):
        """General invariant: whatever window_days is, nothing older than
        it should ever appear - started or fresh."""
        pod = self.fx.podcast("Show C")
        self.fx.episode(pod, "fresh, in window", self.now - datetime.timedelta(days=5))
        self.fx.episode(
            pod, "started, way outside window", self.now - datetime.timedelta(days=90),
            playhead=5, playstate=1, last_played=self.now - datetime.timedelta(hours=1),
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        cutoff = self.now - datetime.timedelta(days=21)
        for e in queue:
            self.assertGreaterEqual(e["pubdate"], cutoff,
                                     f"{e['title']!r} is older than the {21}-day window")

    def test_playstate_2_does_not_exclude_an_episode(self):
        """Regression for the 2026-09-10 bug: ZPLAYSTATE=2 does NOT mean
        'finished' in this schema - confirmed wrong when a blanket
        ZPLAYSTATE != 2 filter silently dropped 16 genuinely-unplayed
        episodes (ZLASTDATEPLAYED was NULL on every one of them) from
        real reports. An episode with ZUNPLAYEDTAB=1 and ZPLAYSTATE=2
        must still appear."""
        pod = self.fx.podcast("Show D")
        self.fx.episode(
            pod, "playstate 2 but never actually played",
            self.now - datetime.timedelta(days=1),
            playstate=2, playhead=0, last_played=None, unplayedtab=1,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertIn("playstate 2 but never actually played", self.titles(queue))

    def test_backcatalog_recovers_episode_missing_unplayedtab(self):
        """Regression for the 2026-09-10 undercount bug: a show whose
        episodes never got ZUNPLAYEDTAB set (Elevate with Robert Glazer,
        after being re-followed) still shows if ZBACKCATALOG=1."""
        pod = self.fx.podcast("Show E")
        self.fx.episode(
            pod, "backcatalog import, no unplayedtab flag",
            self.now - datetime.timedelta(days=5),
            unplayedtab=0, backcatalog=1,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertIn("backcatalog import, no unplayedtab flag", self.titles(queue))

    def test_neither_flag_set_excludes_episode(self):
        """An episode with neither ZUNPLAYEDTAB nor ZBACKCATALOG set is
        not part of the unplayed queue - this is the normal case for the
        vast majority of a show's back-catalog that Rob has already
        listened to (or that simply never entered the tab)."""
        pod = self.fx.podcast("Show F")
        self.fx.episode(
            pod, "neither flag set",
            self.now - datetime.timedelta(days=1),
            unplayedtab=0, backcatalog=0,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertNotIn("neither flag set", self.titles(queue))

    def test_excludes_unentitled_episodes(self):
        """A paid-subscriber-exclusive episode metadata-cached without
        entitlement (ZENTITLEMENTSTATE=2) should never appear - Apple's own
        UI won't surface content it won't let you play."""
        pod = self.fx.podcast("Show G")
        self.fx.episode(
            pod, "patreon bonus", self.now - datetime.timedelta(days=1),
            entitlement=2,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertNotIn("patreon bonus", self.titles(queue))

    def test_excludes_unsubscribed_podcasts(self):
        pod = self.fx.podcast("Unsubscribed Show", subscribed=False)
        self.fx.episode(pod, "orphaned episode", self.now - datetime.timedelta(days=1))
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertNotIn("orphaned episode", self.titles(queue))

    def test_cross_promo_zero_duration_with_disclaimer_excluded(self):
        pod = self.fx.podcast("Show H")
        disclaimer = ("This podroll episode is not affiliated with, endorsed by, "
                       "or produced in conjunction with the host podcast feed.")
        self.fx.episode(
            pod, "You Might Also Like: Some Other Show",
            self.now - datetime.timedelta(days=1), duration=0, description=disclaimer,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertNotIn("You Might Also Like: Some Other Show", self.titles(queue))

    def test_zero_duration_without_disclaimer_kept(self):
        """A brand-new episode whose audio enclosure Apple hasn't finished
        processing yet also has ZDURATION=0, but has no cross-promo
        disclaimer in its description - it must NOT be filtered out just
        because duration is zero (the original, too-broad fix for this)."""
        pod = self.fx.podcast("Show I")
        self.fx.episode(
            pod, "brand new, still processing", self.now - datetime.timedelta(hours=1),
            duration=0, description="A normal episode description.",
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertIn("brand new, still processing", self.titles(queue))

    def test_stuck_zero_duration_no_asset_excluded_after_grace_period(self):
        """Regression for the 2026-09-10 'UP NEXT - 0 seconds left to
        finish' bug: a zero-duration episode with no asset at all, still
        stuck 20 days after publish, must be excluded."""
        pod = self.fx.podcast("Marketing Over Coffee")
        self.fx.episode(
            pod, "Lauren Esposito on Workforce Orchestration!",
            self.now - datetime.timedelta(days=20),
            duration=0, asset_url=None, byte_size=0,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertNotIn("Lauren Esposito on Workforce Orchestration!", self.titles(queue))

    def test_zero_duration_no_asset_kept_within_grace_period(self):
        """A brand-new episode with no asset yet (still processing) is
        kept for the first 24 hours - same 'doesn't have one yet' vs
        'will never have one' distinction as the duration/disclaimer
        filter, just gated on time instead of a disclaimer."""
        pod = self.fx.podcast("CodePen Radio")
        self.fx.episode(
            pod, "Brand new upload, still processing",
            self.now - datetime.timedelta(hours=1),
            duration=0, asset_url=None, byte_size=0,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertIn("Brand new upload, still processing", self.titles(queue))

    def test_zero_duration_with_asset_kept(self):
        """A zero-duration episode that DOES have an asset (bytesize/url
        present) is a different situation entirely and is not touched by
        the stuck-asset exclusion."""
        pod = self.fx.podcast("Show J")
        self.fx.episode(
            pod, "zero duration but has an asset",
            self.now - datetime.timedelta(days=20),
            duration=0, asset_url="https://example.com/a.mp3", byte_size=999,
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertIn("zero duration but has an asset", self.titles(queue))

    def test_started_episodes_dedup_per_podcast(self):
        """Only one started-but-unfinished episode per podcast is pinned,
        even if a show somehow has two in progress."""
        pod = self.fx.podcast("Show K")
        self.fx.episode(
            pod, "started first", self.now - datetime.timedelta(days=2),
            playhead=10, playstate=1, last_played=self.now - datetime.timedelta(hours=2),
        )
        self.fx.episode(
            pod, "started second", self.now - datetime.timedelta(days=1),
            playhead=10, playstate=1, last_played=self.now - datetime.timedelta(hours=1),
        )
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        started_titles = self.titles(queue) & {"started first", "started second"}
        self.assertEqual(len(started_titles), 1)

    def test_fresh_episodes_are_not_deduped(self):
        """Multiple never-started episodes from the same podcast inside the
        window all show - no per-podcast cap on fresh episodes."""
        pod = self.fx.podcast("Show L")
        self.fx.episode(pod, "fresh one", self.now - datetime.timedelta(days=2))
        self.fx.episode(pod, "fresh two", self.now - datetime.timedelta(days=1))
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertTrue({"fresh one", "fresh two"} <= self.titles(queue))

    def test_queue_sorted_newest_first(self):
        pod = self.fx.podcast("Show M")
        self.fx.episode(pod, "older", self.now - datetime.timedelta(days=10))
        self.fx.episode(pod, "newer", self.now - datetime.timedelta(days=1))
        queue = ps.get_unplayed_queue(self.fx.cur, self.now, window_days=21)
        self.assertEqual([e["title"] for e in queue], ["newer", "older"])


class GetDuplicateEpisodesTests(unittest.TestCase):
    def _entry(self, title, pubdate, podcast="Some Show"):
        return {"title": title, "podcast": podcast, "pubdate": pubdate}

    def test_groups_exact_title_matches_across_shows(self):
        pub = datetime.datetime(2026, 9, 1)
        queue = [
            self._entry("Petra Mathers and Her Michael | From Death of an Artist", pub, "Hot Money"),
            self._entry("Petra Mathers and Her Michael | From Death of an Artist", pub, "Lost Hills"),
            self._entry("Unrelated Episode", pub, "Some Other Show"),
        ]
        dupes = ps.get_duplicate_episodes(queue)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(len(dupes[0]), 2)

    def test_whitespace_and_case_normalized(self):
        pub = datetime.datetime(2026, 9, 1)
        queue = [
            self._entry("  Same   Title  ", pub, "Show A"),
            self._entry("same title", pub, "Show B"),
        ]
        dupes = ps.get_duplicate_episodes(queue)
        self.assertEqual(len(dupes), 1)

    def test_no_false_positives_for_distinct_titles(self):
        pub = datetime.datetime(2026, 9, 1)
        queue = [self._entry("A", pub), self._entry("B", pub)]
        self.assertEqual(ps.get_duplicate_episodes(queue), [])


class GradeForDaysTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(ps.grade_for_days(0), "A+")
        self.assertEqual(ps.grade_for_days(0.03), "A")
        self.assertEqual(ps.grade_for_days(0.5), "A-")
        self.assertEqual(ps.grade_for_days(1), "A-")
        self.assertEqual(ps.grade_for_days(7), "C-")
        self.assertEqual(ps.grade_for_days(10), "D-")
        self.assertEqual(ps.grade_for_days(11), "F")
        self.assertEqual(ps.grade_for_days(365), "F")


class CoreDataDateTests(unittest.TestCase):
    def test_roundtrip(self):
        original = datetime.datetime(2026, 9, 10, 12, 0, 0)
        self.assertEqual(ps.cd_to_dt(ps.dt_to_cd(original)), original)


if __name__ == "__main__":
    unittest.main()
