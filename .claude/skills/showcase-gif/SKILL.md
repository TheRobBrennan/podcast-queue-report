---
name: showcase-gif
description: Make a shareable animated GIF that tours a rendered HTML report (e.g. reports/daily_flow.html), pausing on each section with a highlight and a caption explaining what it is or what's new. Use when asked for a GIF/demo/walkthrough of the daily flow or podcast report.
---

# Showcase GIF

Turns a rendered HTML report into a narrated GIF: the camera glides to each
section, dims the rest, outlines it, and shows a caption bar (pill badge +
heading + two detail lines).

## Steps

1. Regenerate the report first so the GIF shows live data (`make flow`, or
   `make html` for the queue report) - never a stale file.
2. Pick or write a scenes file in `scenes/`. `scenes/daily_flow.json` covers
   `reports/daily_flow.html`. Each scene is `{rect:[x0,y0,x1,y1], pill,
   color (gray|green|indigo), title, lines:[..]}` with `rect` in page pixels
   at 900px width. Use green + `NEW` for things that changed recently (check
   `git log`), and keep caption numbers in sync with the report.
3. If the layout moved, find the new rects:
   `python3 .claude/skills/showcase-gif/make_showcase_gif.py probe reports/daily_flow.html`
4. Render (output goes in `~/Downloads` unless told otherwise):
   `python3 .claude/skills/showcase-gif/make_showcase_gif.py render reports/daily_flow.html .claude/skills/showcase-gif/scenes/daily_flow.json ~/Downloads/daily_flow_showcase.gif`
5. Look at a few frames (pill and heading share one centre line; captions
   not clipped) before handing it over; target a few MB.

Needs Google Chrome and ffmpeg; Pillow is installed on first run into
`~/.cache/showcase-gif-venv`. The repo path starts with U+F8FF - glob it
(`~/repos/*Podcasts`) rather than typing it.
