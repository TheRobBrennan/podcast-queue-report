"""Unit tests for flow.py: history, day/week analysis, and the tick decision.

Everything here is deterministic - synthetic snapshots with fixed
timestamps, an explicit America/New_York zone, no database or network.
"""
import datetime
import json
import os
import tempfile
import unittest
from zoneinfo import ZoneInfo

import flow

NY = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc


def snap(local_iso, label="manual", queue_count=120, queue_seconds=300000,
         behind=900000, played_count=0, played_seconds=0, since_local_iso=None):
    ts = datetime.datetime.fromisoformat(local_iso).replace(tzinfo=NY).astimezone(UTC)
    since = (
        datetime.datetime.fromisoformat(since_local_iso).replace(tzinfo=NY).astimezone(UTC)
        if since_local_iso else ts - datetime.timedelta(hours=2)
    )
    return {
        "ts": ts.replace(tzinfo=None).isoformat(),
        "since": since.replace(tzinfo=None).isoformat(),
        "label": label, "source": "test",
        "queue_count": queue_count, "queue_seconds": queue_seconds,
        "behind_seconds": behind, "grade": "D",
        "played_count": played_count, "played_seconds": played_seconds,
    }


def now_ny(iso):
    return datetime.datetime.fromisoformat(iso).replace(tzinfo=NY).astimezone(UTC)


class HistoryTests(unittest.TestCase):
    def test_round_trip_and_sorting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.jsonl")
            b = snap("2026-10-16T12:00", label="evening")
            a = snap("2026-10-16T09:00", label="morning")
            flow.append_snapshot(b, path)
            flow.append_snapshot(a, path)
            rows = flow.load_history(path)
            self.assertEqual([r["label"] for r in rows], ["morning", "evening"])
            self.assertEqual(flow.load_history(os.path.join(tmp, "missing")), [])

    def test_snapshot_from_run_reads_the_report_json(self):
        d = {
            "generated_at": "2026-10-16T18:06:22.5",
            "last_run_iso": "2026-10-16T16:06:20.1",
            "queue": {"count": 122, "total_seconds": 329437.0, "days_behind": 9.5, "grade": "D"},
            "played": {"since_last_run": {"count": 4, "total_seconds": 10158.0}},
        }
        s = flow.snapshot_from_run(d, "morning")
        self.assertEqual(
            (s["queue_count"], s["queue_seconds"], s["behind_seconds"],
             s["played_count"], s["played_seconds"], s["label"]),
            (122, 329437, 820800, 4, 10158, "morning"),
        )
        self.assertEqual(s["since"], "2026-10-16T16:06:20.1")


class DayTests(unittest.TestCase):
    def test_day_runs_4am_to_4am_local(self):
        late = snap("2026-10-17T00:30", label="evening")
        early = snap("2026-10-17T05:00", label="morning")
        history = [late, early]
        self.assertEqual(len(flow.day_snapshots(history, datetime.date(2026, 10, 16), NY)), 1)
        self.assertEqual(len(flow.day_snapshots(history, datetime.date(2026, 10, 17), NY)), 1)

    def test_listening_queue_and_arrivals(self):
        history = [
            snap("2026-10-16T09:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T12:00", "manual", 118, 295000, 880000, 3, 7200),
            snap("2026-10-16T22:00", "evening", 119, 298000, 870000, 5, 10800),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["listened_seconds"], 18000)
        self.assertEqual(s["listened_count"], 8)
        self.assertEqual(s["queue_count_delta"], -1)
        self.assertEqual(s["queue_seconds_delta"], -2000)
        self.assertEqual(s["behind_delta"], -30000)
        # (295000-300000)+7200 = 2200 ; (298000-295000)+10800 = 13800
        self.assertEqual(s["arrivals_seconds"], 16000)

    def test_arrivals_never_negative(self):
        a = snap("2026-10-16T09:00", queue_seconds=300000)
        b = snap("2026-10-16T11:00", queue_seconds=250000, played_seconds=1000)
        self.assertEqual(flow.arrivals_seconds(a, b), 0)

    def test_dash_window_pairs_start_and_end(self):
        history = [
            snap("2026-10-16T10:00", "dash-start", 120, 300000, 900000, 1, 600),
            snap("2026-10-16T13:00", "manual", 119, 297000, 890000, 2, 5400),
            snap("2026-10-16T18:00", "dash-end", 117, 290000, 860000, 3, 9000),
            snap("2026-10-16T22:00", "evening", 118, 292000, 850000, 1, 1200),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual(len(s["dashes"]), 1)
        dash = s["dashes"][0]
        # the start snapshot's own window is before the Dash, so it is excluded
        self.assertEqual(dash["listened_seconds"], 5400 + 9000)
        self.assertEqual(dash["queue_seconds_delta"], -10000)
        self.assertEqual(dash["queue_count_delta"], -3)
        self.assertEqual(dash["behind_delta"], -40000)

    def test_unpaired_dash_start_is_ignored(self):
        history = [snap("2026-10-16T10:00", "dash-start")]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["dashes"], [])

    def test_longest_drought_spans_consecutive_silent_windows(self):
        history = [
            snap("2026-10-16T09:00", played_seconds=500, since_local_iso="2026-10-16T07:00"),
            snap("2026-10-16T11:00", played_seconds=0, since_local_iso="2026-10-16T09:00"),
            snap("2026-10-16T14:00", played_seconds=0, since_local_iso="2026-10-16T11:00"),
            snap("2026-10-16T16:00", played_seconds=900, since_local_iso="2026-10-16T14:00"),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        span, start, end = s["drought"]
        self.assertEqual(span, 5 * 3600)
        self.assertTrue(start.startswith("2026-10-16T13:00"))

    def test_drought_is_clipped_to_the_start_of_the_day(self):
        # the overnight window began at 10pm the evening before; only the
        # time after 4am counts toward this day's quietest stretch
        history = [
            snap("2026-10-16T05:00", "morning", played_seconds=0),
            snap("2026-10-16T08:00", played_seconds=0, since_local_iso="2026-10-15T22:00"),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["drought"][0], 4 * 3600)

    def test_empty_day(self):
        s = flow.summarize_day([], datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["snapshots"], [])
        self.assertIn("No snapshots", flow.render_day_chat(s, NY))


class WeekTests(unittest.TestCase):
    def test_week_totals_and_best_day(self):
        history = [
            snap("2026-10-12T08:00", "morning", 131, 345000, 905000, 0, 0),
            snap("2026-10-12T21:00", "evening", 130, 340000, 900000, 5, 20000),
            snap("2026-10-13T08:00", "morning", 130, 340000, 900000, 0, 0),
            snap("2026-10-13T21:00", "evening", 128, 330000, 880000, 6, 30000),
            snap("2026-10-14T08:00", "morning", 128, 330000, 880000, 0, 0),
            snap("2026-10-14T21:00", "evening", 125, 320000, 860000, 4, 10000),
        ]
        w = flow.summarize_week(history, datetime.date(2026, 10, 14), NY)
        self.assertEqual(len(w["days"]), 3)
        self.assertEqual(w["listened_seconds"], 60000)
        self.assertEqual(w["average_seconds"], 20000)
        self.assertEqual(w["best"]["day"], datetime.date(2026, 10, 13))
        self.assertEqual(w["queue_end"]["queue_count"], 125)
        self.assertEqual(flow.week_days(datetime.date(2026, 10, 14))[0], datetime.date(2026, 10, 12))


class TickTests(unittest.TestCase):
    def test_no_morning_snapshot_by_11am_triggers_the_fallback(self):
        self.assertEqual(flow.decide_tick([], now_ny("2026-10-16T11:06"), NY), "morning")

    def test_before_11am_takes_only_a_quiet_reading(self):
        self.assertEqual(flow.decide_tick([], now_ny("2026-10-16T09:06"), NY), "pulse")

    def test_existing_morning_snapshot_suppresses_the_fallback(self):
        history = [snap("2026-10-16T08:00", "morning")]
        self.assertEqual(flow.decide_tick(history, now_ny("2026-10-16T15:06"), NY), "pulse")
        history = [snap("2026-10-16T08:00", "morning"), snap("2026-10-16T14:30", "manual")]
        self.assertIsNone(flow.decide_tick(history, now_ny("2026-10-16T15:06"), NY))  # 36 minutes since

    def test_late_afternoon_with_no_morning_snapshot_is_a_quiet_reading_not_a_morning(self):
        self.assertEqual(flow.decide_tick([], now_ny("2026-10-16T18:06"), NY), "pulse")

    def test_quiet_readings_come_about_every_hour_and_not_in_the_small_hours(self):
        h = [snap("2026-10-16T08:00", "morning"), snap("2026-10-16T12:06", "pulse")]
        self.assertIsNone(flow.decide_tick(h, now_ny("2026-10-16T12:36"), NY))
        self.assertEqual(flow.decide_tick(h, now_ny("2026-10-16T13:06"), NY), "pulse")
        self.assertIsNone(flow.decide_tick(h, now_ny("2026-10-17T03:06"), NY))

    def test_evening_fallback_at_10pm(self):
        history = [snap("2026-10-16T08:00", "morning")]
        self.assertEqual(flow.decide_tick(history, now_ny("2026-10-16T22:06"), NY), "evening")

    def test_evening_after_midnight_belongs_to_the_closing_day(self):
        history = [snap("2026-10-16T08:00", "morning"), snap("2026-10-17T00:10", "evening")]
        self.assertIsNone(flow.decide_tick(history, now_ny("2026-10-17T01:06"), NY))

    def test_a_mac_that_slept_all_day_still_closes_the_day(self):
        self.assertEqual(flow.decide_tick([], now_ny("2026-10-16T22:06"), NY), "evening")

    def test_minimum_gap_between_snapshots(self):
        history = [snap("2026-10-16T11:00", "manual")]
        self.assertIsNone(flow.decide_tick(history, now_ny("2026-10-16T11:10"), NY))

    def test_follows_the_zone_it_is_given(self):
        la = ZoneInfo("America/Los_Angeles")
        # 11:06pm New York is 8:06pm Los Angeles: not evening yet there
        now = now_ny("2026-10-16T23:06")
        self.assertEqual(flow.decide_tick([], now, NY), "evening")
        self.assertEqual(flow.decide_tick([], now, la), "pulse")


def minimal_run_json():
    """Just enough of podcast_summary's output for render_report.build_html."""
    return {
        "generated_at": "2026-10-16T20:06:22.5",
        "last_run_iso": "2026-10-16T18:06:23.3",
        "queue": {
            "count": 0, "count_fmt": "0", "total_seconds": 0, "total_fmt": "0 seconds",
            "oldest_date": None, "days_behind": 0, "days_behind_amount": "0 seconds",
            "days_behind_phrase": "right on time", "grade": "A", "episodes": [],
            "up_next_remaining_fmt": "0 seconds",
        },
        "played": {"since_last_run": {"count": 0, "count_fmt": "0", "total_seconds": 0,
                                      "total_fmt": "0 seconds"}},
        "duplicates": [], "now_playing": None,
        "emoji_header": "x", "emoji_list": ["x"],
        "config": {"email": "", "phone": "", "signoff_name": "", "label": "Podcast Queue Report",
                   "timezone": "America/New_York"},
    }


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.history = [
            snap("2026-10-15T08:00", "morning", 119, 280000, 800000, 0, 0),
            snap("2026-10-15T09:00", "evening", 119, 280000, 800000, 2, 3000),
            snap("2026-10-16T09:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T12:00", "manual", 118, 295000, 880000, 3, 7200),
            snap("2026-10-16T22:00", "evening", 119, 298000, 870000, 5, 10800),
        ]
        self.history[2]["now_playing"] = {"title": "Pregame", "podcast": "Kraken Audio"}
        self.history[3]["by_show"] = [{"podcast": "Kraken Audio", "count": 59, "seconds": 90000, "oldest": "2026-10-06T07:00:00"},
                                      {"podcast": "RedHanded", "count": 30, "seconds": 40000, "oldest": "2026-10-09T07:00:00"}]
        self.s = flow.summarize_day(self.history, datetime.date(2026, 10, 16), NY)

    def test_parse_fmt_seconds(self):
        self.assertEqual(flow.parse_fmt_seconds("1 hr 4 mins 5 seconds"), 3845)
        self.assertEqual(flow.parse_fmt_seconds("1\xa0week 2\xa0days"), 604800 + 172800)
        self.assertEqual(flow.parse_fmt_seconds(""), 0)

    def test_every_show_is_listed_oldest_first_even_with_one_episode(self):
        d = {"queue": {"episodes": [
            {"podcast": "New Show", "duration_fmt": "30 mins", "pubdate": "2026-10-15T07:00:00", "artwork_url": "https://a/n.jpg"},
            {"podcast": "Old Show", "duration_fmt": "1 hr", "pubdate": "2026-10-06T07:00:00", "podcast_url": "https://p/o"},
            {"podcast": "Old Show", "duration_fmt": "30 mins", "pubdate": "2026-10-09T07:00:00"},
            {"podcast": "Middle Show", "duration_fmt": "10 mins", "pubdate": "2026-10-10T07:00:00"},
        ]}}
        rows = flow.queue_by_show(d)
        self.assertEqual([r["podcast"] for r in rows], ["Old Show", "Middle Show", "New Show"])
        self.assertEqual((rows[0]["count"], rows[0]["seconds"], rows[0]["oldest"]), (2, 5400, "2026-10-06T07:00:00"))
        self.assertEqual(rows[2]["count"], 1)

    def test_pile_section_lists_every_show_with_age_artwork_and_links(self):
        d = {"queue": {"episodes": [
            {"podcast": "Old Show", "duration_fmt": "1 hr", "pubdate": "2026-10-06T07:00:00",
             "artwork_url": "https://a/o.jpg", "podcast_url": "https://p/o"},
            {"podcast": "Single Show", "duration_fmt": "10 mins", "pubdate": "2026-10-15T07:00:00"},
        ]}}
        s = {"by_show_snapshot": None}
        page = flow.piling_up_html(s, d)
        for needle in ("2 shows, 2 episodes", "Old Show", "Single Show", 'src="https://a/o.jpg"',
                       'href="https://p/o"', "oldest waiting"):
            self.assertIn(needle, page)
        self.assertLess(page.index("Old Show"), page.index("Single Show"))

    def test_no_by_show_data_means_no_section(self):
        self.assertEqual(flow.piling_up_html({"by_show_snapshot": None}), "")

    def test_queue_by_show_groups_and_ranks(self):
        d = {"queue": {"episodes": [
            {"podcast": "A", "duration_fmt": "1 hr"}, {"podcast": "A", "duration_fmt": "30 mins"},
            {"podcast": "B", "duration_fmt": "10 mins"}]}}
        shows = flow.queue_by_show(d)
        self.assertEqual([(r["podcast"], r["count"], r["seconds"]) for r in shows],
                         [("A", 2, 5400), ("B", 1, 600)])

    def test_snapshot_records_now_playing_and_by_show(self):
        d = minimal_run_json()
        d["now_playing"] = {"title": "T", "podcast": "P"}
        d["queue"]["episodes"] = [{"podcast": "P", "duration_fmt": "5 mins"}]
        sn = flow.snapshot_from_run(d, "morning")
        self.assertEqual(sn["now_playing"], {"title": "T", "podcast": "P"})
        self.assertEqual(sn["by_show"][0]["count"], 1)

    def test_windows_carry_queue_change_and_arrivals(self):
        w = self.s["windows"]
        self.assertIsNone(w[0]["queue_delta"])
        self.assertEqual(w[1]["queue_delta"], -2)
        self.assertEqual(self.s["biggest_listen"]["played"], 10800)

    def test_context_compares_to_yesterday_at_the_same_time_of_day(self):
        self.assertEqual(self.s["yesterday_listened"], 3000)
        lines = " ".join(line for _, line in flow.vs_lines(self.s, NY))
        self.assertIn("You've listened 4 hrs 10 mins more than yesterday at this time", lines)
        self.assertNotIn("your usual", lines)  # one earlier day is not an average

    def test_an_average_needs_two_earlier_days_and_stops_at_the_same_hour(self):
        history = [
            snap("2026-10-14T08:00", "morning"),
            snap("2026-10-14T12:00", "manual", played_seconds=3600),
            snap("2026-10-14T21:00", "evening", played_seconds=36000),   # after today's cutoff: ignored
            snap("2026-10-15T08:00", "morning"),
            snap("2026-10-15T12:00", "manual", played_seconds=7200),
            snap("2026-10-15T21:00", "evening", played_seconds=36000),   # ignored too
            snap("2026-10-16T08:00", "morning"),
            snap("2026-10-16T12:00", "manual", played_seconds=1800),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["yesterday_listened"], 7200)
        self.assertEqual(s["recent_days"], 2)
        self.assertEqual(s["recent_average"], 5400)
        lines = " ".join(line for _, line in flow.vs_lines(s, NY))
        self.assertIn("You've listened 1 hr 30 mins less than yesterday at this time", lines)
        self.assertIn("You're 1 hr below your usual listening at this time", lines)

    def test_now_playing_card_has_artwork_links_progress_and_up_next(self):
        np = {"title": "The Nightmare Cure", "podcast": "MrBallen", "episode_url": "https://e/1",
              "podcast_url": "https://p/1", "artwork_url": "https://a/1.jpg",
              "elapsed_seconds": 600, "duration_seconds": 1200, "remaining_fmt": "10 mins"}
        nxt = {"title": "Next One", "podcast": "Other Show", "episode_url": "https://e/2",
               "podcast_url": "https://p/2", "artwork_url": "https://a/2.jpg", "duration_fmt": "45 mins"}
        card = flow.now_playing_card_html(np, nxt)
        for needle in ('src="https://a/1.jpg"', 'href="https://e/1"', 'href="https://p/1"',
                       "50% played", "10 mins left", "UP NEXT", 'href="https://e/2"', 'href="https://p/2"',
                       'src="https://a/2.jpg"', 'width="40"', "45 mins"):
            self.assertIn(needle, card)
        self.assertEqual(flow.now_playing_card_html(None), "")
        self.assertIn("NOW PLAYING", flow.now_playing_card_html({"title": "Only a title"}))

    def test_snapshot_keeps_the_now_playing_details(self):
        d = minimal_run_json()
        d["now_playing"] = {"title": "T", "podcast": "P", "episode_url": "u", "artwork_url": "a",
                            "elapsed_seconds": 5, "duration_seconds": 10, "remaining_fmt": "5 seconds"}
        d["queue"]["episodes"] = [{"title": "Q", "podcast": "P2", "duration_fmt": "5 mins",
                                   "pubdate": "2026-10-10T07:00:00", "in_progress": False}]
        sn = flow.snapshot_from_run(d, "pulse")
        self.assertEqual(sn["now_playing"]["artwork_url"], "a")
        self.assertEqual(sn["up_next"]["title"], "Q")

    def test_a_quiet_stretch_inside_a_dash_is_not_called_nap_time(self):
        history = [
            snap("2026-10-16T09:00", "morning", played_seconds=100, since_local_iso="2026-10-16T07:00"),
            snap("2026-10-16T10:00", "dash-start", played_seconds=100, since_local_iso="2026-10-16T09:00"),
            snap("2026-10-16T12:00", "manual", played_seconds=0, since_local_iso="2026-10-16T10:00"),
            snap("2026-10-16T15:00", "manual", played_seconds=0, since_local_iso="2026-10-16T12:00"),
            snap("2026-10-16T18:00", "dash-end", played_seconds=500, since_local_iso="2026-10-16T15:00"),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        text = " ".join(line for _, line in flow.highlights(s, NY))
        self.assertIn("Quiet stretch mid-Dash", text)
        self.assertNotIn("Nap time", text)

    def test_keycaps_spell_the_count(self):
        self.assertEqual(flow.keycaps(121), "1\ufe0f\u20e32\ufe0f\u20e31\ufe0f\u20e3")

    def test_queue_card_is_green_yellow_or_red(self):
        def card(start, end):
            return flow.queue_card(snap("2026-10-16T09:00", queue_count=start),
                                   snap("2026-10-16T18:00", queue_count=end), NY)
        self.assertIn("#dcfce7", card(124, 121))
        self.assertIn("▼ 3 episodes since 9:00am EDT", card(124, 121))
        self.assertIn("#fef9c3", card(121, 121))
        self.assertIn("No change since 9:00am EDT", card(121, 121))
        self.assertIn("#fee2e2", card(121, 124))
        self.assertIn("▲ 3 episodes since 9:00am EDT", card(121, 124))

    def test_listening_counts_from_the_first_reading_of_the_day(self):
        # the first reading's window reaches back before the day's first reading
        history = [
            snap("2026-10-16T09:00", "morning", 120, 300000, 900000, 4, 9999),
            snap("2026-10-16T12:00", "manual", 118, 295000, 880000, 3, 7200),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual((s["listened_seconds"], s["listened_count"]), (7200, 3))
        self.assertEqual(flow.window_sentence(s["windows"][0]), "First reading of the day · 120 episodes in the queue")
        self.assertEqual(s["biggest_listen"]["snap"]["label"], "manual")

    def test_every_card_says_since_the_same_time(self):
        s = flow.summarize_day(self.history, datetime.date(2026, 10, 16), NY)
        page = flow.render_day_html(s, NY)
        self.assertEqual(page.count("since 9:00am EDT"), 4)  # listened, queue, oldest, arrivals
        self.assertIn("episodes played since 9:00am EDT", page)

    def test_oldest_episode_line_is_green_yellow_or_red_like_the_queue(self):
        since = "since 9:00am EDT"
        better = flow.age_line(-18000, since)
        self.assertIn("#15803d", better)
        self.assertIn("▼ Improved 5 hrs", better)
        self.assertIn("#a16207", flow.age_line(0, since))
        self.assertIn("▬ No change", flow.age_line(0, since))
        worse = flow.age_line(7200, since)
        self.assertIn("#b91c1c", worse)
        self.assertIn("▲ Slipped 2 hrs", worse)

    def test_every_card_value_is_the_same_size_and_never_wraps(self):
        for text in ("6 hrs 21 mins", "1 week 2 days 10 hrs 48 mins"):
            v = flow.fit_value(text)
            self.assertIn(f"font-size:{flow.CARD_VALUE_SIZE}px", v)
            self.assertIn("white-space:nowrap", v)

    def test_arrivals_count_is_queue_change_plus_played(self):
        a = snap("2026-10-16T09:00", queue_count=120)
        b = snap("2026-10-16T11:00", queue_count=118, played_count=3)
        self.assertEqual(flow.arrivals_count(a, b), 1)
        c = snap("2026-10-16T13:00", queue_count=100, played_count=1)
        self.assertEqual(flow.arrivals_count(b, c), 0)

    def test_arrivals_line_is_blue_yellow_or_red(self):
        since = "since 9:00am EDT"
        self.assertIn("#1d4ed8", flow.arrivals_line(2, 3000, 10000, since))   # listened more than arrived: blue
        self.assertIn("#a16207", flow.arrivals_line(5, 10200, 10000, since))  # about even
        red = flow.arrivals_line(9, 20000, 10000, since)
        self.assertIn("#b91c1c", red)
        self.assertIn("▲ 9 episodes since 9:00am EDT", red)
        self.assertIn("▲ 1 episode since", flow.arrivals_line(1, 100, 10000, since))

    def test_biggest_stretch_and_burst_show_start_and_end_and_counts(self):
        history = [
            snap("2026-10-16T09:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T12:00", "manual", 118, 295000, 880000, 3, 7200, "2026-10-16T09:00"),
            snap("2026-10-16T14:00", "manual", 121, 310000, 880000, 1, 600, "2026-10-16T12:00"),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        text = " ".join(line for _, line in flow.highlights(s, NY))
        self.assertIn("Biggest listening stretch: 2 hrs, 9:00am to 12:00pm", text)
        # 121-118 played 1 = 4 episodes, 310000-295000+600 = 15600s
        self.assertIn("Biggest arrival burst: 4 episodes (4 hrs 20 mins), 12:00pm to 2:00pm", text)

    def test_listening_by_part_of_day_and_the_peak(self):
        history = [
            snap("2026-10-16T08:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T10:00", "manual", 119, 299000, 890000, 1, 1800),
            snap("2026-10-16T15:00", "manual", 117, 290000, 880000, 4, 9000),
            snap("2026-10-16T22:30", "evening", 118, 292000, 870000, 1, 900),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        # the 10:00pm-ish window (20:30 to 22:30) is split 75/25 between evening and late night
        self.assertEqual([p["name"] for p in s["by_part"]], ["afternoon", "morning", "evening", "late night"])
        self.assertEqual({p["name"]: p["seconds"] for p in s["by_part"]},
                         {"afternoon": 9000, "morning": 1800, "evening": 675, "late night": 225})
        text = " ".join(line for _, line in flow.highlights(s, NY))
        self.assertIn("Your peak was afternoon (12pm to 5pm): 2 hrs 30 mins of 3 hrs 15 mins (77%)", text)

    def test_a_window_that_straddles_noon_is_split_between_morning_and_afternoon(self):
        history = [
            snap("2026-10-16T08:00", "morning", played_seconds=0),
            snap("2026-10-16T14:00", "manual", played_seconds=6000, since_local_iso="2026-10-16T10:00"),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual({p["name"]: p["seconds"] for p in s["by_part"]}, {"morning": 3000, "afternoon": 3000})

    def test_pace_line(self):
        history = [
            snap("2026-10-16T08:00", "morning", 120, 350000, 900000, 0, 0),
            snap("2026-10-16T20:00", "evening", 100, 300000, 880000, 10, 30000),
        ]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        # shrank 50000s in 12h = 100000s/day; 300000s left = 3 days
        self.assertIn("clear the queue in about 3 days", flow.pace_line(s)[1])
        history[1] = snap("2026-10-16T20:00", "evening", 125, 360000, 900000, 2, 3000)
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertIn("isn't shrinking", flow.pace_line(s)[1])
        short = flow.summarize_day(history[:1] + [snap("2026-10-16T09:00", "manual")], datetime.date(2026, 10, 16), NY)
        self.assertIsNone(flow.pace_line(short))

    def test_heading_has_the_full_date_with_a_lowercase_ordinal(self):
        self.assertEqual(flow.pretty_day_full(datetime.date(2026, 10, 16)), "Friday, Oct 16th, 2026")
        self.assertEqual(flow.pretty_day_full(datetime.date(2026, 10, 22)), "Thursday, Oct 22nd, 2026")
        self.assertEqual(flow.upper_keep_ordinal("Daily Flow — Friday, Oct 16th, 2026"),
                         "DAILY FLOW — FRIDAY, OCT 16th, 2026")
        page = flow.render_day_html(self.s, NY)
        self.assertIn("DAILY FLOW — FRIDAY, OCT 16th, 2026", page)

    def test_labels_have_names_and_approximate_ones_are_marked(self):
        self.assertEqual(flow.label_name({"label": "dash-end"}), "🏁 Dash end")
        self.assertEqual(flow.label_name({"label": "manual"}), "📸 Check-in")
        self.assertTrue(flow.label_name({"label": "morning", "approx": True}).endswith("≈"))

    def test_story_shows_up_in_every_format(self):
        chat = flow.render_day_chat(self.s, NY)
        for needle in ("What's piling up", "Kraken Audio", "☀️ Morning", "🌙 Evening", "Biggest listening stretch"):
            self.assertIn(needle, chat)
        page = flow.render_day_html(self.s, NY)
        for needle in ("What&rsquo;s piling up", "Pregame", "The oldest unplayed episode in your queue"):
            self.assertIn(needle, page)
        for gone in ("added up across today", "start of day to now", "how old your oldest",
                     "new episodes that landed", "estimated"):
            self.assertNotIn(gone, page)


class GeneratedAndQueueTests(unittest.TestCase):
    def setUp(self):
        self.history = [
            snap("2026-10-16T09:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T10:00", "dash-start", 120, 300000, 900000, 1, 600),
            snap("2026-10-16T18:00", "dash-end", 117, 290000, 860000, 3, 9000),
        ]
        self.s = flow.summarize_day(self.history, datetime.date(2026, 10, 16), NY)
        self.generated = datetime.datetime(2026, 10, 16, 20, 18, tzinfo=UTC)

    def test_long_stamp_matches_the_main_report_format(self):
        self.assertEqual(flow.long_stamp(self.generated, NY), "Friday, October 16th, 2026 @ 4:18pm EDT")

    def test_web_report_has_the_generated_pill_at_top_and_bottom(self):
        page = flow.render_day_html(self.s, NY, self.generated)
        self.assertEqual(page.count("Generated Friday, October 16th, 2026 @ 4:18pm EDT"), 2)

    def test_the_whole_queue_is_in_the_page_not_collapsed(self):
        d = minimal_run_json()
        d["queue"]["episodes"] = [
            {"title": f"Episode {i}", "podcast": "Show", "duration_fmt": "10 mins",
             "pubdate": "2026-10-10T07:00:00", "in_progress": False}
            for i in range(30)
        ]
        d["queue"]["count"] = 30
        page = flow.render_day_html(self.s, NY, self.generated, d)
        self.assertEqual(sum(f"Episode {i}" in page for i in range(30)), 30)
        self.assertNotIn("<details", page)

    def test_queue_section_is_appended_without_a_second_cast_row(self):
        page = flow.render_day_html(self.s, NY, self.generated, minimal_run_json())
        self.assertIn("The unplayed queue", page)
        self.assertIn("all caught up", page)
        self.assertEqual(page.count("<html"), 1)
        self.assertEqual(page.count("</body>"), 1)
        self.assertEqual(page.count("display:inline-block;margin:0 2px;"), 21)  # one emoji row only

    def test_discord_day_embed(self):
        payload = flow.build_discord_day(self.s, self.generated, NY)
        embed = payload["embeds"][0]
        self.assertIn("🐷", embed["title"])
        self.assertIn("That papa listened to", embed["description"])
        self.assertIn("Generated Friday, October 16th, 2026 @ 4:18pm EDT", embed["description"])
        self.assertEqual(embed["timestamp"], "2026-10-16T20:18:00")
        names = [f["name"] for f in embed["fields"]]
        self.assertTrue(any("Dash 10:00am" in n for n in names))
        self.assertTrue(any("The shape of your day" in n for n in names))

    def test_discord_week_embed(self):
        w = flow.summarize_week(self.history, datetime.date(2026, 10, 16), NY)
        embed = flow.build_discord_week(w, self.generated, NY)["embeds"][0]
        self.assertIn("Weekly Flow", embed["description"])
        self.assertTrue(any("By day" in f["name"] for f in embed["fields"]))

    def test_email_is_off_unless_asked_for(self):
        old = os.environ.pop("FLOW_EMAIL", None)
        try:
            self.assertFalse(flow._flow_email_enabled())
            os.environ["FLOW_EMAIL"] = "1"
            self.assertTrue(flow._flow_email_enabled())
        finally:
            os.environ.pop("FLOW_EMAIL", None)
            if old is not None:
                os.environ["FLOW_EMAIL"] = old


class TagDashTests(unittest.TestCase):
    def setUp(self):
        self.history = [
            snap("2026-10-16T08:00", "morning"),
            snap("2026-10-16T10:06", "pulse"),
            snap("2026-10-16T12:06", "pulse"),
            snap("2026-10-16T14:06", "pulse"),
            snap("2026-10-16T18:06", "pulse"),
            snap("2026-10-16T22:00", "evening"),
        ]
        self.day = datetime.date(2026, 10, 16)

    def test_parse_clock(self):
        self.assertEqual(flow.parse_clock("10:11am"), datetime.time(10, 11))
        self.assertEqual(flow.parse_clock("6:18pm"), datetime.time(18, 18))
        self.assertEqual(flow.parse_clock("12:05am"), datetime.time(0, 5))
        self.assertEqual(flow.parse_clock("18:18"), datetime.time(18, 18))
        self.assertEqual(flow.parse_clock("10am"), datetime.time(10, 0))
        with self.assertRaises(ValueError):
            flow.parse_clock("soon")

    def test_tags_the_nearest_ordinary_readings(self):
        rows, msgs, ok = flow.tag_dash(self.history, self.day, datetime.time(10, 11), datetime.time(18, 18), NY)
        self.assertTrue(ok)
        labels = {flow.local_dt(r, NY).strftime("%H:%M"): r["label"] for r in rows}
        self.assertEqual(labels["10:06"], "dash-start")
        self.assertEqual(labels["18:06"], "dash-end")
        self.assertTrue(all(r.get("approx") for r in rows if r["label"].startswith("dash")))
        self.assertEqual(labels["08:00"], "morning")
        self.assertEqual(labels["22:00"], "evening")
        # the original history is untouched until the caller writes it
        self.assertTrue(all(r["label"] != "dash-start" for r in self.history))

    def test_never_overwrites_a_morning_or_evening_reading(self):
        history = [snap("2026-10-16T10:00", "morning"), snap("2026-10-16T12:00", "pulse"),
                   snap("2026-10-16T18:00", "evening")]
        rows, msgs, ok = flow.tag_dash(history, self.day, datetime.time(10, 11), datetime.time(18, 18), NY, 120)
        self.assertFalse(ok)  # the only candidates near the end are the evening reading and the 12:00 pulse is too far
        self.assertEqual(rows, history)

    def test_nothing_tagged_when_no_reading_is_close(self):
        history = [snap("2026-10-16T07:00", "pulse"), snap("2026-10-16T21:00", "pulse")]
        rows, msgs, ok = flow.tag_dash(history, self.day, datetime.time(10, 11), datetime.time(18, 18), NY, 90)
        self.assertFalse(ok)
        self.assertIn("no ordinary reading within 90 minutes", " ".join(msgs))
        self.assertEqual(rows, history)

    def test_end_must_follow_start(self):
        _, msgs, ok = flow.tag_dash(self.history, self.day, datetime.time(18, 0), datetime.time(10, 0), NY)
        self.assertFalse(ok)

    def test_a_tagged_dash_shows_up_in_the_day(self):
        rows, _, _ = flow.tag_dash(self.history, self.day, datetime.time(10, 11), datetime.time(18, 18), NY)
        s = flow.summarize_day(rows, self.day, NY)
        self.assertEqual(len(s["dashes"]), 1)


class WindowStoryTests(unittest.TestCase):
    def window(self, played_count, played, qd, arrivals, q=120):
        return {"snap": {"queue_count": q, "played_count": played_count}, "played": played,
                "queue_delta": qd, "arrivals_count": arrivals, "first": False}

    def test_listening_inside_an_unfinished_episode_is_not_nothing(self):
        def with_np(local, elapsed):
            sn = snap(local, "manual", 121, 300000, 900000, 0, 0)
            sn["now_playing"] = {"title": "Long Episode", "podcast": "Show", "elapsed_seconds": elapsed,
                                 "duration_seconds": 1000}
            return sn
        history = [with_np("2026-10-16T09:00", 200), with_np("2026-10-16T10:00", 600),
                   with_np("2026-10-16T11:00", 100), with_np("2026-10-16T12:00", 110)]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        stories = [flow.window_story(w)[0] for w in s["windows"][1:]]
        self.assertEqual(stories[0], "Kept listening: moved ahead 6 mins 40 seconds in the current episode (20% → 60%). The queue stayed at 121 episodes")
        self.assertEqual(stories[1], "Replayed: went back 8 mins 20 seconds in the current episode (60% → 10%). The queue stayed at 121 episodes")
        self.assertTrue(stories[2].startswith("No episodes finished"))  # moved 10 seconds: not worth a sentence

    def test_artwork_for_an_episode_is_borrowed_from_a_later_reading(self):
        early = snap("2026-10-16T09:00", "manual")
        early["now_playing"] = {"title": "Ep", "podcast": "Show"}               # recorded before artwork was saved
        later = snap("2026-10-16T10:00", "manual")
        later["now_playing"] = {"title": "Ep", "podcast": "Show", "artwork_url": "https://a/9.jpg",
                                "episode_url": "https://e/9"}
        history = [snap("2026-10-16T08:00", "morning"), early, later]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        page = flow.render_day_html(s, NY)
        row = page.split("The shape of your day")[1]
        self.assertIn('src="https://a/9.jpg"', row.split("What&rsquo;s piling up")[0])

    def test_what_was_playing_is_shown_only_when_it_changes(self):
        playing = {"title": "Same Episode", "podcast": "Show", "artwork_url": "https://a/1.jpg"}
        history = [snap("2026-10-16T08:00", "morning"), snap("2026-10-16T09:00"), snap("2026-10-16T10:00"),
                   snap("2026-10-16T11:00")]
        for h in history[1:3]:
            h["now_playing"] = dict(playing)
        history[3]["now_playing"] = dict(playing, title="Different Episode")
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        page = flow.render_day_html(s, NY)
        self.assertEqual(page.count("Same Episode"), 1)
        self.assertEqual(page.count("▶ Playing"), 2)  # one row per change: Same, then Different

    def test_window_label_adds_am_pm_only_when_the_window_crosses_noon(self):
        a = snap("2026-10-16T14:00", since_local_iso="2026-10-16T12:00")
        b = snap("2026-10-16T12:06", since_local_iso="2026-10-16T10:06")
        self.assertEqual(flow.window_label(a, NY), "12:00\u20132:00pm")
        self.assertEqual(flow.window_label(b, NY), "10:06am\u201312:06pm")

    def test_listening_that_shrinks_the_queue(self):
        text, tone, delta = flow.window_story(self.window(3, 9000, -3, 0, 122))
        self.assertEqual(text, "Listened to 3 episodes (2 hrs 30 mins). The queue decreased from 125 to 122 episodes")
        self.assertEqual((tone, delta), ("better", -3))

    def test_listening_outpaced_by_arrivals(self):
        text, tone, delta = flow.window_story(self.window(1, 2400, 2, 3, 126))
        self.assertEqual(text, "Listened to 1 episode (40 mins). The queue increased from 124 to 126 episodes")
        self.assertEqual(tone, "worse")

    def test_arrivals_keeping_pace(self):
        text, tone, _ = flow.window_story(self.window(2, 3000, 0, 2, 121))
        self.assertEqual(text, "Listened to 2 episodes (50 mins). The queue stayed at 121 episodes")
        self.assertEqual(tone, "same")

    def test_a_quiet_window(self):
        text, tone, _ = flow.window_story(self.window(0, 0, 0, 0, 121))
        self.assertEqual(text, "No episodes finished. The queue stayed at 121 episodes")
        text, tone, _ = flow.window_story(self.window(0, 0, 3, 3, 124))
        self.assertEqual(text, "No episodes finished. The queue increased from 121 to 124 episodes")

    def test_timeline_shows_the_window_the_artwork_and_the_dash_in_words(self):
        history = [
            snap("2026-10-16T08:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T10:00", "dash-start", 120, 300000, 900000, 1, 600),
            snap("2026-10-16T18:00", "dash-end", 117, 290000, 860000, 3, 9000),
        ]
        history[2]["now_playing"] = {"title": "Pregame", "podcast": "Kraken Audio", "episode_url": "https://e/1",
                                     "podcast_url": "https://p/1", "artwork_url": "https://a/1.jpg"}
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        page = flow.render_day_html(s, NY)
        self.assertIn("4:00\u20136:00pm", page)
        self.assertIn("8:00\u201310:00am", page)
        self.assertIn("Listened to 3 episodes (2 hrs 30 mins). The queue decreased from 120 to 117 episodes", page)
        self.assertIn('src="https://a/1.jpg"', page)
        self.assertIn('href="https://e/1"', page)
        self.assertIn("you listened to 3 episodes (2 hrs 30 mins), the queue got 2 hrs 46 mins shorter, and your oldest episode improved by 11 hrs 6 mins", page)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.history = [
            snap("2026-10-16T09:00", "morning", 120, 300000, 900000, 0, 0),
            snap("2026-10-16T10:00", "dash-start", 120, 300000, 900000, 1, 600),
            snap("2026-10-16T18:00", "dash-end", 117, 290000, 860000, 3, 9000),
            snap("2026-10-16T22:00", "evening", 118, 292000, 850000, 1, 1200),
        ]
        self.s = flow.summarize_day(self.history, datetime.date(2026, 10, 16), NY)

    def test_chat_mentions_the_dash_and_the_totals(self):
        text = flow.render_day_chat(self.s, NY)
        self.assertIn("Daily Flow — Friday, Oct 16", text)
        self.assertIn("🚐 Dash 10:00am–6:00pm", text)
        self.assertIn("Listened:", text)

    def test_the_whole_cast_is_in_every_report(self):
        for emoji in ("🐷", "🦄", "🏝️"):
            self.assertIn(emoji, flow.render_day_chat(self.s, NY))
            self.assertIn(emoji, flow.render_day_html(self.s, NY))

    def test_headline_has_the_papa_voice(self):
        self.assertIn("That papa listened to", flow.day_headline(self.s))
        self.assertEqual(flow.mood(-100), "")
        self.assertNotIn("furballs are proud", flow.day_headline(self.s))
        self.assertTrue(flow.day_headline(self.s).endswith("today 🎧") or "backlog" in flow.day_headline(self.s) or "held" in flow.day_headline(self.s))
        self.assertIn("held the line", flow.mood(0))
        self.assertIn("the backlog crept up", flow.mood(100))

    def test_html_is_a_page_and_escapes(self):
        page = flow.render_day_html(self.s, NY)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("The shape of your day", page)
        self.assertIn("Dash 10:00am–6:00pm", page)

    def test_browser_tab_title_is_title_cased(self):
        page = flow.render_day_html(self.s, NY)
        self.assertIn("<title>Daily Flow — Friday, Oct 16th, 2026 · Podcast Queue Report</title>", page)
        self.assertNotIn("Daily flow", page)

    def test_email_payload_matches_send_email_layout(self):
        payload = flow.email_payload("Subject here", "<p>x</p>")
        first, _, rest = payload.partition("\n")
        self.assertEqual(first, "SUBJECT: Subject here")
        self.assertTrue(rest.startswith("---\n"))

    def test_week_render(self):
        w = flow.summarize_week(self.history, datetime.date(2026, 10, 16), NY)
        self.assertIn("Weekly Flow", flow.render_week_chat(w, NY))
        self.assertIn("Weekly Flow", flow.render_week_html(w, NY))


if __name__ == "__main__":
    unittest.main()
