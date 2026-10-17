#!/usr/bin/env python3
"""Turn a rendered HTML report into a narrated, animated GIF.

  make_showcase_gif.py probe  REPORT.html            # list block positions (page px)
  make_showcase_gif.py render REPORT.html SCENES.json OUT.gif

The camera glides to each scene, dims everything but its rectangle, outlines
it, and shows a caption bar (pill + heading + detail lines). Needs Google
Chrome, ffmpeg and Pillow (auto-installed into ~/.cache/showcase-gif-venv).
"""
import json, os, shutil, subprocess, sys, tempfile

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VENV = os.path.expanduser("~/.cache/showcase-gif-venv")
W, VH, CAP, FPS, MOVE, HOLD, SCALE = 900, 540, 150, 10, 1.0, 4.2, 0.8
COLORS = {"gray": (120, 126, 140), "green": (34, 170, 110), "indigo": (99, 102, 241)}


def chrome(*args):
    return subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                           "--force-device-scale-factor=1", *args],
                          capture_output=True, text=True).stdout


def probe(html):
    src = open(html).read()
    js = ("<script>addEventListener('load',()=>{let o=[];document.querySelectorAll('body *').forEach(e=>{"
          "const r=e.getBoundingClientRect();if(r.width>300&&r.height>40)o.push([e.tagName,Math.round(r.left),"
          "Math.round(r.top+scrollY),Math.round(r.left+r.width),Math.round(r.top+scrollY+r.height),"
          "(e.innerText||'').slice(0,40).replace(/\\n/g,' ')].join(' | '))});"
          "document.body.innerHTML='<pre>'+o.join('\\n')+'</pre>'})</script>")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(src.replace("</body>", js + "</body>"))
    out = chrome(f"--window-size={W},6000", "--virtual-time-budget=3000", "--dump-dom", "file://" + f.name)
    import re, html as h
    print("tag | x0 | y0 | x1 | y1 | text")
    print(h.unescape(re.sub(r"<[^>]+>", "", out)))


def render(html, scenes_path, out_gif):
    if not os.path.exists(VENV + "/bin/python"):
        subprocess.check_call([sys.executable, "-m", "venv", VENV])
        subprocess.check_call([VENV + "/bin/pip", "-q", "install", "pillow"])
    if os.path.realpath(sys.prefix) != os.path.realpath(VENV):
        os.execv(VENV + "/bin/python", [VENV + "/bin/python", *sys.argv])
    from PIL import Image, ImageDraw, ImageFont

    scenes = json.load(open(scenes_path))
    tmp = tempfile.mkdtemp()
    shot = tmp + "/full.png"
    height = max(s["rect"][3] for s in scenes) + VH
    chrome(f"--screenshot={shot}", f"--window-size={W},{height}", "file://" + os.path.abspath(html))
    page = Image.open(shot).convert("RGB")
    fonts = "/System/Library/Fonts/Helvetica.ttc"
    f_title, f_body, f_pill = (ImageFont.truetype(fonts, s, index=i) for s, i in ((26, 1), (18, 0), (14, 1)))
    bg = (17, 20, 30)

    def caption(d, sc, col):
        d.rectangle([0, VH, W, VH + CAP], fill=bg)
        d.rectangle([0, VH, W, VH + 3], fill=col)
        bb = d.textbbox((0, 0), sc["pill"], font=f_pill)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        cy = VH + 36                                   # shared vertical centre of pill + heading
        px0, px1, py0, py1 = 28, 28 + tw + 28, cy - th / 2 - 8, cy + th / 2 + 8
        d.rounded_rectangle([px0, py0, px1, py1], (py1 - py0) / 2, fill=col)
        d.text(((px0 + px1) / 2 - bb[0] - tw / 2, cy - bb[1] - th / 2), sc["pill"], font=f_pill, fill="white")
        tb = d.textbbox((0, 0), sc["title"], font=f_title)  # centre heading on its cap/x-height box
        d.text((px1 + 14 - tb[0], cy - (tb[1] + tb[3]) / 2), sc["title"], font=f_title, fill="white")
        for i, line in enumerate(sc["lines"]):
            d.text((28, VH + 72 + i * 28), line, font=f_body, fill=(200, 205, 220))

    def frame(cam, rect, sc):
        col = COLORS.get(sc.get("color", "gray"), COLORS["gray"])
        view = page.crop((0, int(cam), W, int(cam) + VH)).convert("RGBA")
        ov = Image.new("RGBA", view.size, (10, 14, 28, 150))
        x0, y0, x1, y1 = rect[0] - 8, rect[1] - cam - 8, rect[2] + 8, rect[3] - cam + 8
        ImageDraw.Draw(ov).rounded_rectangle([x0, y0, x1, y1], 14, fill=(0, 0, 0, 0))
        view = Image.alpha_composite(view, ov)
        ImageDraw.Draw(view).rounded_rectangle([x0, y0, x1, y1], 14, width=3,
                                               outline=col + (255,) if sc.get("color", "gray") != "gray" else "white")
        out = Image.new("RGB", (W, VH + CAP), bg)
        out.paste(view.convert("RGB"), (0, 0))
        caption(ImageDraw.Draw(out), sc, col)
        return out.resize((int(W * SCALE), int((VH + CAP) * SCALE)), Image.LANCZOS)

    ease = lambda t: min(max(t, 0), 1) ** 2 * (3 - 2 * min(max(t, 0), 1))
    cam_for = lambda r: max(0, r[1] - 15 if r[3] - r[1] > VH - 30 else r[1] - (VH - (r[3] - r[1])) // 2)
    n, prev = 0, None
    os.makedirs(tmp + "/f")
    for sc in scenes:
        r1, c1 = sc["rect"], cam_for(sc["rect"])
        r0, c0, sc0 = prev or (r1, c1, sc)
        for k in range(int(MOVE * FPS)):
            e = ease((k + 1) / (MOVE * FPS))
            frame(c0 + (c1 - c0) * e, [a + (b - a) * e for a, b in zip(r0, r1)], sc if e > .5 else sc0).save(f"{tmp}/f/{n:04d}.png"); n += 1
        hold = frame(c1, r1, sc)
        for k in range(int(HOLD * FPS)):
            hold.save(f"{tmp}/f/{n:04d}.png"); n += 1
        prev = (r1, c1, sc)
    subprocess.check_call(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", tmp + "/f/%04d.png", "-vf",
                           "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
                           "[b][p]paletteuse=dither=none:diff_mode=rectangle", out_gif])
    shutil.rmtree(tmp)
    print(f"{out_gif}: {n} frames, {os.path.getsize(out_gif) / 1e6:.1f} MB")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "probe":
        probe(a[1])
    elif len(a) == 4 and a[0] == "render":
        render(*a[1:])
    else:
        sys.exit(__doc__)
