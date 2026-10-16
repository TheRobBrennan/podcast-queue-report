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

    def test_before_11am_waits_for_a_real_trigger(self):
        self.assertIsNone(flow.decide_tick([], now_ny("2026-10-16T09:06"), NY))

    def test_existing_morning_snapshot_suppresses_the_fallback(self):
        history = [snap("2026-10-16T08:00", "morning")]
        self.assertIsNone(flow.decide_tick(history, now_ny("2026-10-16T15:06"), NY))

    def test_late_afternoon_with_no_morning_snapshot_does_nothing(self):
        self.assertIsNone(flow.decide_tick([], now_ny("2026-10-16T18:06"), NY))

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
        self.assertIsNone(flow.decide_tick([], now, la))


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
        self.history[3]["by_show"] = [{"podcast": "Kraken Audio", "count": 59, "seconds": 90000},
                                      {"podcast": "RedHanded", "count": 30, "seconds": 40000}]
        self.s = flow.summarize_day(self.history, datetime.date(2026, 10, 16), NY)

    def test_parse_fmt_seconds(self):
        self.assertEqual(flow.parse_fmt_seconds("1 hr 4 mins 5 seconds"), 3845)
        self.assertEqual(flow.parse_fmt_seconds("1\xa0week 2\xa0days"), 604800 + 172800)
        self.assertEqual(flow.parse_fmt_seconds(""), 0)

    def test_show_groups_fold_feeds_into_one_slice(self):
        groups = flow.load_show_groups("🏒 Kraken=Seattle Kraken Audio Network|Beneath The Waves;Other=zzz")
        self.assertEqual(groups[0][0], "🏒 Kraken")
        by_show = [
            {"podcast": "Seattle Kraken Audio Network", "count": 25, "seconds": 36000},
            {"podcast": "Beneath The Waves: A Seattle Kraken Podcast", "count": 4, "seconds": 14000},
            {"podcast": "RedHanded", "count": 6, "seconds": 9000},
        ]
        grouped = flow.group_shows(by_show, groups)
        self.assertEqual((grouped[0]["name"], grouped[0]["count"], grouped[0]["seconds"], grouped[0]["feeds"]),
                         ("🏒 Kraken", 29, 50000, 2))
        self.assertEqual(grouped[1]["name"], "RedHanded")

    def test_slices_add_up_to_the_whole_with_an_everything_else(self):
        by_show = [{"podcast": f"Show {i}", "count": 1, "seconds": 1000} for i in range(8)]
        segs, total = flow.pile_segments({"by_show": by_show}, groups=[], top=4)
        self.assertEqual(len(segs), 5)
        self.assertTrue(segs[-1].get("other"))
        self.assertAlmostEqual(sum(s["pct"] for s in segs), 100.0)
        self.assertIn("Clear all of Show", flow.pile_insight(segs))

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

    def test_context_compares_to_yesterday_and_recent_average(self):
        self.assertEqual(self.s["yesterday_listened"], 3000)
        self.assertEqual(self.s["recent_average"], 3000)
        lines = " ".join(line for _, line in flow.vs_lines(self.s))
        self.assertIn("more than yesterday", lines)
        self.assertIn("above your recent average", lines)

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
        self.assertEqual(flow.window_sentence(s["windows"][0]), "First reading of the day · queue 120")
        self.assertEqual(s["biggest_listen"]["snap"]["label"], "manual")

    def test_every_card_says_since_the_same_time(self):
        s = flow.summarize_day(self.history, datetime.date(2026, 10, 16), NY)
        page = flow.render_day_html(s, NY)
        self.assertEqual(page.count("since 9:00am EDT"), 3)
        self.assertIn("episodes played since 9:00am EDT", page)

    def test_labels_have_names_and_approximate_ones_are_marked(self):
        self.assertEqual(flow.label_name({"label": "dash-end"}), "🏁 Dash end")
        self.assertEqual(flow.label_name({"label": "manual"}), "📸 Check-in")
        self.assertTrue(flow.label_name({"label": "morning", "approx": True}).endswith("≈"))

    def test_story_shows_up_in_every_format(self):
        chat = flow.render_day_chat(self.s, NY)
        for needle in ("What's piling up", "Kraken Audio", "☀️ Morning", "🌙 Evening", "Biggest listening stretch", "Clear all of"):
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
        self.assertIn("<title>Daily Flow — Friday, Oct 16 · Podcast Queue Report</title>", page)
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
