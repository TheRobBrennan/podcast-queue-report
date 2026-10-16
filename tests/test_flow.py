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
        history = [snap("2026-10-16T08:00", played_seconds=0, since_local_iso="2026-10-15T22:00")]
        s = flow.summarize_day(history, datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["drought"][0], 4 * 3600)

    def test_empty_day(self):
        s = flow.summarize_day([], datetime.date(2026, 10, 16), NY)
        self.assertEqual(s["snapshots"], [])
        self.assertIn("No snapshots", flow.render_day_chat(s, NY))


class WeekTests(unittest.TestCase):
    def test_week_totals_and_best_day(self):
        history = [
            snap("2026-10-12T21:00", "evening", 130, 340000, 900000, 5, 20000),
            snap("2026-10-13T21:00", "evening", 128, 330000, 880000, 6, 30000),
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
        self.assertIn("Daily flow — Friday, Oct 16", text)
        self.assertIn("🚐 Dash 10:00am–6:00pm", text)
        self.assertIn("Listened:", text)

    def test_html_is_a_page_and_escapes(self):
        page = flow.render_day_html(self.s, NY)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("Dash windows", page)

    def test_email_payload_matches_send_email_layout(self):
        payload = flow.email_payload("Subject here", "<p>x</p>")
        first, _, rest = payload.partition("\n")
        self.assertEqual(first, "SUBJECT: Subject here")
        self.assertTrue(rest.startswith("---\n"))

    def test_week_render(self):
        w = flow.summarize_week(self.history, datetime.date(2026, 10, 16), NY)
        self.assertIn("Weekly flow", flow.render_week_chat(w, NY))
        self.assertIn("Weekly flow", flow.render_week_html(w, NY))


if __name__ == "__main__":
    unittest.main()
