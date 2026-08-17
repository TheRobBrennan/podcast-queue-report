#!/usr/bin/env python3
"""Regenerates assets/grade-colors.png — the swatch sheet of every grade
badge shown in the README.

The swatches import render_report's own color functions rather than
re-listing the palette, so the sheet can't drift from what the report
actually renders. Writes the intermediate HTML next to the PNG and shoots it
with headless Chrome; if Chrome isn't where this expects it, the HTML is
still written and can be screenshotted by hand.

    python3 scripts/make_grade_swatches.py
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import render_report as r  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ANCHORS = ("A+", "A", "C", "D", "F")


def build_html():
    rows = ""
    for g in r.GRADE_SCALE:
        hexv, txt = r.grade_hex(g), r.grade_text_color(g)
        is_anchor = g in ANCHORS
        rows += f'''
    <tr class="{"anchor" if is_anchor else ""}">
      <td><span class="grade-badge" style="background:{hexv};color:{txt}">{g}</span></td>
      <td class="hex">{hexv}</td>
      <td class="kind">{"anchor" if is_anchor else "blend"}</td>
      <td class="bar"><span style="background:{hexv}"></span></td>
    </tr>'''

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Grade colors</title><style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; margin: 0; padding: 32px; color: #1e293b; }}
  .container {{ max-width: 560px; margin: 0 auto; background: white; border-radius: 12px; padding: 24px 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 17px; text-transform: uppercase; letter-spacing: 0.5px; color: #64748b; margin: 0 0 4px; }}
  .sub {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }}
  tr.anchor td {{ background: #f8fafc; }}
  .grade-badge {{ display: inline-block; border-radius: 8px; padding: 2px 12px; font-weight: 700; font-size: 15px; min-width: 24px; text-align: center; }}
  .hex {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; color: #475569; }}
  .kind {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.4px; }}
  .bar {{ width: 220px; }}
  .bar span {{ display: block; height: 16px; border-radius: 4px; }}
  .foot {{ margin-top: 18px; font-size: 12px; color: #64748b; line-height: 1.5; }}
</style></head><body><div class="container">
<h1>Grade badge colors</h1>
<div class="sub">One continuous green &rarr; yellow &rarr; red ramp. Shaded rows are the chosen anchors; the rest are linear blends of their neighbors.</div>
<table>{rows}</table>
<div class="foot">Badge text color is derived from each badge\'s own WCAG relative luminance &mdash; white where it clears 4:1, dark slate across the yellow/lime middle where it doesn\'t.</div>
</div></body></html>'''


def main():
    assets = os.path.join(REPO, "assets")
    os.makedirs(assets, exist_ok=True)
    html_path = os.path.join(assets, "grade-colors.html")
    png_path = os.path.join(assets, "grade-colors.png")

    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(build_html())
    print("wrote", html_path)

    if not os.path.exists(CHROME):
        print("Chrome not found at %s — screenshot %s by hand." % (CHROME, html_path))
        return 0

    subprocess.run([
        CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2", "--window-size=760,700",
        "--screenshot=" + png_path, "file://" + html_path,
    ], check=True, capture_output=True)
    print("wrote", png_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
