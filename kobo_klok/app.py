import hashlib
import io
import json
import math
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Configuration (Home Assistant App /data/options.json + SUPERVISOR_TOKEN,
# with full fallback to standalone Docker environment variables)
# ---------------------------------------------------------------------------
_ha_opts: dict = {}
_opts_path = Path("/data/options.json")
if _opts_path.exists():
    try:
        _ha_opts = json.loads(_opts_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[Config] Could not read /data/options.json: {exc}", file=sys.stderr)

ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", Path(__file__).parent / "assets"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_URL = os.environ.get("HA_URL", "http://supervisor/core" if SUPERVISOR_TOKEN else "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", SUPERVISOR_TOKEN)
PORT = int(os.environ.get("PORT", "8099"))
REFRESH_SECONDS = int(_ha_opts.get("refresh_seconds", os.environ.get("REFRESH_SECONDS", "300")))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "15"))
EINK_BOOST = bool(_ha_opts.get("eink_boost", os.environ.get("EINK_BOOST", "1") == "1"))

PAGE_W = int(_ha_opts.get("screen_width", os.environ.get("SCREEN_WIDTH", "1072")))
PAGE_H = int(_ha_opts.get("screen_height", os.environ.get("SCREEN_HEIGHT", "1448")))
CLOCK_SIZE = min(PAGE_W, PAGE_H)

MEDIA_ENABLED = bool(_ha_opts.get("media_enabled", os.environ.get("MEDIA_ENABLED", "0") == "1"))
MEDIA_POSTER_ENTITY = str(_ha_opts.get("media_poster_entity", os.environ.get("MEDIA_POSTER_ENTITY", "")) or "").strip()
MEDIA_GAMING_SENSOR = str(_ha_opts.get("media_gaming_sensor", os.environ.get("MEDIA_GAMING_SENSOR", "")) or "").strip()
MEDIA_GAMING_IMAGE = str(_ha_opts.get("media_gaming_image", os.environ.get("MEDIA_GAMING_IMAGE", "")) or "").strip()
MEDIA_SERVICE_SENSOR = str(_ha_opts.get("media_service_sensor", os.environ.get("MEDIA_SERVICE_SENSOR", "")) or "").strip()

INACTIVE_STATES = {"", "idle", "off", "standby", "unknown", "unavailable", "none"}

SERVICE_META = {
    "disney": (
        "Disney+",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3e/Disney%2B_logo.svg/330px-Disney%2B_logo.svg.png",
    ),
    "f1tv": (
        "F1 TV",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/330px-F1.svg.png",
    ),
    "hbo_max": (
        "HBO Max",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/5/59/HBO_Max_2025.svg/960px-HBO_Max_2025.svg.png",
    ),
    "jellyfin": (
        "Jellyfin",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a2/Jellyfin_logo.svg/330px-Jellyfin_logo.svg.png",
    ),
    "netflix": (
        "Netflix",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/0/08/Netflix_2015_logo.svg/330px-Netflix_2015_logo.svg.png",
    ),
    "prime": (
        "Prime Video",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/1/11/Amazon_Prime_Video_logo.svg/330px-Amazon_Prime_Video_logo.svg.png",
    ),
    "videoland": (
        "Videoland",
        "https://upload.wikimedia.org/wikipedia/commons/7/72/VIDEOLAND_BY_RTL.png",
    ),
    "youtube": (
        "YouTube",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b8/YouTube_Logo_2017.svg/330px-YouTube_Logo_2017.svg.png",
    ),
    "ziggo": (
        "Ziggo GO",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2d/Ziggo.svg/330px-Ziggo.svg.png",
    ),
    "npo_start": (
        "NPO Start",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a9/NPO_Start_logo.svg/330px-NPO_Start_logo.svg.png",
    ),
}

HAND_PIVOTS = {"iron": 0.7438, "brass": 0.7627}
HAND_COLORS = {"iron": (28, 26, 23), "brass": (184, 146, 74)}

DEFAULT_SECTIONS = [
    {"name": "Thuis", "key": "thuis", "states": "home,Thuis"},
    {"name": "Werk", "key": "werk", "states": "Werk,Work"},
    {"name": "School", "key": "school", "states": "School"},
    {"name": "Familie", "key": "familie", "states": "Familie,Family"},
    {"name": "Onderweg", "key": "onderweg", "states": "Onderweg,Travelling"},
    {"name": "Elders", "key": "elders", "states": "not_home,away,Elders"},
]

DEFAULT_PERSONS = [
    {"name": "Alex", "entity": "person.alex", "person_entity": "person.alex", "hand": "iron"},
    {"name": "Sam", "entity": "person.sam", "person_entity": "person.sam", "hand": "brass"},
]


def _parse_sections(raw_sections: list) -> List[dict]:
    parsed = []
    for sec in raw_sections:
        raw_states = sec.get("states", "")
        if isinstance(raw_states, str):
            states = [s.strip() for s in raw_states.split(",") if s.strip()]
        else:
            states = [str(s).strip() for s in raw_states if str(s).strip()]
        parsed.append({
            "name": str(sec.get("name", "Elders")),
            "key": str(sec.get("key", sec.get("name", "elders"))).lower(),
            "states": states,
        })
    return parsed or _parse_sections(DEFAULT_SECTIONS)


def _parse_persons(raw_persons: list) -> List[dict]:
    parsed = []
    for idx, p in enumerate(raw_persons):
        hand = str(p.get("hand", "brass" if idx % 2 == 1 else "iron")).lower()
        if hand not in ("iron", "brass"):
            hand = "brass" if idx % 2 == 1 else "iron"
        entity = str(p.get("entity", "")).strip()
        person_entity = str(p.get("person_entity") or entity).strip()
        parsed.append({
            "name": str(p.get("name", f"Persoon {idx + 1}")),
            "entity": entity,
            "person_entity": person_entity,
            "hand": hand,
            "color": HAND_COLORS[hand],
        })
    return parsed or _parse_persons(DEFAULT_PERSONS)


SECTIONS = _parse_sections(_ha_opts.get("sections") or DEFAULT_SECTIONS)
PERSONS = _parse_persons(_ha_opts.get("persons") or DEFAULT_PERSONS)

DAYS_NL = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
MONTHS_NL = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

_avatar_cache: Dict[str, Tuple[float, Image.Image]] = {}
_media_img_cache: Dict[str, Tuple[float, Image.Image]] = {}
_render_cache: Tuple[float, str, bytes] = (0.0, "", b"")


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def ha_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def fetch_ha_state(entity_id: str) -> Optional[dict]:
    if not entity_id or not HA_URL or not HA_TOKEN:
        return None
    try:
        r = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=ha_headers(), timeout=5)
        if r.ok:
            return r.json()
    except Exception as exc:
        print(f"[HA] Failed to fetch {entity_id}: {exc}", file=sys.stderr)
    return None


def fetch_avatar(entity_picture: Optional[str]) -> Optional[Image.Image]:
    if not entity_picture or not HA_URL or not HA_TOKEN:
        return None
    now = time.time()
    cached = _avatar_cache.get(entity_picture)
    if cached and (now - cached[0]) < 3600:
        return cached[1]
    url = entity_picture if entity_picture.startswith("http") else f"{HA_URL}{entity_picture}"
    try:
        r = requests.get(url, headers=ha_headers(), timeout=5)
        if r.ok:
            im = Image.open(io.BytesIO(r.content)).convert("RGBA")
            _avatar_cache[entity_picture] = (now, im)
            return im
    except Exception as exc:
        print(f"[HA] Failed to fetch avatar {url}: {exc}", file=sys.stderr)
    return None


def resolve_section_index(raw_state: str) -> int:
    low = (raw_state or "").strip().lower()
    for i, sec in enumerate(SECTIONS):
        if sec["name"].lower() == low:
            return i
        if any(s.lower() == low for s in sec["states"]):
            return i
    return len(SECTIONS) - 1


def ray_square_dist(deg: float, half_size: float) -> Tuple[float, float, float]:
    rad = math.radians(deg)
    dx = math.sin(rad)
    dy = -math.cos(rad)
    m = max(abs(dx), abs(dy), 1e-6)
    return half_size / m, dx, dy


def make_circle_avatar(
    avatar: Optional[Image.Image],
    diameter: int,
    ring_color: Tuple[int, int, int],
    ring_width: int,
    initial: str,
) -> Image.Image:
    size = diameter + ring_width * 2 + 8
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cx = cy = size // 2

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    r_outer = diameter // 2 + ring_width
    sd.ellipse((cx - r_outer + 2, cy - r_outer + 3, cx + r_outer + 2, cy + r_outer + 3), fill=(20, 16, 10, 95))
    shadow = shadow.filter(ImageFilter.GaussianBlur(3.5))
    canvas.alpha_composite(shadow)

    draw = ImageDraw.Draw(canvas)
    draw.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=ring_color + (255,))
    r_inner = diameter // 2
    draw.ellipse((cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner), fill=(242, 236, 224, 255))

    if avatar is not None:
        aw, ah = avatar.size
        side = min(aw, ah)
        cropped = avatar.crop(((aw - side) // 2, (ah - side) // 2, (aw + side) // 2, (ah + side) // 2))
        cropped = cropped.resize((diameter, diameter), Image.Resampling.LANCZOS)
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        canvas.paste(cropped, (cx - r_inner, cy - r_inner), mask)
    else:
        font = load_font(int(diameter * 0.48), bold=True)
        bbox = draw.textbbox((0, 0), initial, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), initial, fill=(40, 35, 28, 255), font=font)

    return canvas


def draw_tracked_text(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    y: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Tuple[int, int, int, int],
    tracking: float = 2.0,
):
    chars = list(text)
    widths = [draw.textbbox((0, 0), ch, font=font)[2] for ch in chars]
    total_w = sum(widths) + tracking * max(0, len(chars) - 1)
    x = center_x - total_w / 2.0
    for ch, cw in zip(chars, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += cw + tracking


def format_since(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        now = datetime.now(dt.tzinfo)
        if dt.date() == now.date():
            return f"sinds {dt.strftime('%H:%M')}"
        return f"sinds {dt.strftime('%d-%m %H:%M')}"
    except Exception:
        return ""


def render_clock_png(demo_mode: bool = False) -> bytes:
    person_states = []
    for idx, p in enumerate(PERSONS):
        if demo_mode or not HA_URL or not HA_TOKEN:
            demo_states = ["home", "School", "Werk", "Onderweg"]
            raw_state = demo_states[idx % len(demo_states)]
            last_changed = datetime.now().isoformat()
            avatar = None
        else:
            s_obj = fetch_ha_state(p["entity"]) or {}
            p_obj = fetch_ha_state(p["person_entity"]) or {}
            raw_state = str(s_obj.get("state") or p_obj.get("state") or "Elders")
            last_changed = s_obj.get("last_changed") or p_obj.get("last_changed")
            pic = (p_obj.get("attributes") or {}).get("entity_picture") or (s_obj.get("attributes") or {}).get("entity_picture")
            avatar = fetch_avatar(pic)
        sec_idx = resolve_section_index(raw_state)
        person_states.append({
            **p,
            "raw_state": raw_state,
            "sec_idx": sec_idx,
            "last_changed": last_changed,
            "avatar": avatar,
        })

    wall = Image.open(ASSETS_DIR / "wall_bg.png").convert("RGBA").resize((CLOCK_SIZE, CLOCK_SIZE), Image.Resampling.LANCZOS)
    bottom_strip = wall.crop((0, CLOCK_SIZE - 4, CLOCK_SIZE, CLOCK_SIZE)).resize((1, 1), Image.Resampling.BILINEAR)
    panel_bg_rgb = bottom_strip.getpixel((0, 0))[:3]

    canvas = Image.new("RGBA", (PAGE_W, PAGE_H), panel_bg_rgb + (255,))
    canvas.paste(wall, (0, 0))

    scale = CLOCK_SIZE / 1000.0
    cx = 501.5 * scale
    cy = 499.5 * scale
    inner_under = 194.0 * scale
    outer_under = 404.0 * scale
    inner_half = 201.0 * scale
    outer_half = 398.0 * scale

    N = len(SECTIONS)
    step = 360.0 / N

    # Layer 2: Digital vakgrenzen & inner crosshair (underneath transparent forged frames)
    div_shadow = Image.new("RGBA", (CLOCK_SIZE, CLOCK_SIZE), (0, 0, 0, 0))
    div_lines = Image.new("RGBA", (CLOCK_SIZE, CLOCK_SIZE), (0, 0, 0, 0))
    ds = ImageDraw.Draw(div_shadow)
    dl = ImageDraw.Draw(div_lines)

    ch_w = max(4, int(round(4.6 * scale)))
    for (x1, y1, x2, y2) in [
        (cx - inner_under, cy, cx + inner_under, cy),
        (cx, cy - inner_under, cx, cy + inner_under),
    ]:
        ds.line((x1 + 4, y1 + 5, x2 + 4, y2 + 5), fill=(42, 34, 22, 85), width=ch_w)
        dl.line((x1, y1, x2, y2), fill=(28, 27, 25, 225), width=ch_w)

    div_w = max(5, int(round(5.8 * scale)))
    for i in range(N):
        div_deg = (i + 0.5) * step
        d_in, dx, dy = ray_square_dist(div_deg, inner_under)
        d_out, _, _ = ray_square_dist(div_deg, outer_under)
        x1, y1 = cx + dx * d_in, cy + dy * d_in
        x2, y2 = cx + dx * d_out, cy + dy * d_out
        ds.line((x1 + 4, y1 + 5, x2 + 4, y2 + 5), fill=(42, 34, 22, 95), width=div_w)
        dl.line((x1, y1, x2, y2), fill=(25, 24, 22, 255), width=div_w)

    div_shadow = div_shadow.filter(ImageFilter.GaussianBlur(3.2))
    canvas.alpha_composite(div_shadow, (0, 0))
    canvas.alpha_composite(div_lines, (0, 0))

    # Layer 3: Forged wrought-iron square frames with realistic drop shadows
    frames = Image.open(ASSETS_DIR / "frames_transparent.png").convert("RGBA").resize((CLOCK_SIZE, CLOCK_SIZE), Image.Resampling.LANCZOS)
    canvas.alpha_composite(frames, (0, 0))

    # Layer 4: Sector glow + icons + tracked uppercase labels
    sector_groups: Dict[int, List[int]] = {}
    for p_idx, ps in enumerate(person_states):
        sector_groups.setdefault(ps["sec_idx"], []).append(p_idx)

    label_font = load_font(int(round(20 * scale)), bold=True)
    icon_px = int(round(92 * scale))
    draw = ImageDraw.Draw(canvas)

    for i, sec in enumerate(SECTIONS):
        deg = i * step
        d_in, dx, dy = ray_square_dist(deg, inner_half)
        d_out, _, _ = ray_square_dist(deg, outer_half)
        r = d_in + (d_out - d_in) * 0.51
        px = cx + dx * r
        py = cy + dy * r
        active = i in sector_groups

        if active:
            glow = Image.new("RGBA", (CLOCK_SIZE, CLOCK_SIZE), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            gr = int(round(88 * scale))
            gd.ellipse((px - gr, py - gr, px + gr, py + gr), fill=(184, 146, 74, 72))
            glow = glow.filter(ImageFilter.GaussianBlur(26))
            canvas.alpha_composite(glow, (0, 0))
            draw = ImageDraw.Draw(canvas)

        icon_path = ASSETS_DIR / f"icon_{sec['key']}.png"
        if icon_path.exists():
            ic = Image.open(icon_path).convert("RGBA").resize((icon_px, icon_px), Image.Resampling.LANCZOS)
            canvas.alpha_composite(ic, (int(round(px - icon_px / 2)), int(round(py - icon_px * 0.64))))

        label_y = py + icon_px * 0.44
        text_color = (18, 16, 13, 255) if active else (46, 41, 35, 240)
        draw_tracked_text(draw, px, label_y, sec["name"].upper(), label_font, text_color, tracking=2.6)

    # Layer 5: Hands + upright circular photo medallions
    for idx, ps in enumerate(person_states):
        sec_idx = ps["sec_idx"]
        group = sector_groups.get(sec_idx, [idx])
        spread = 14.0 if len(group) > 1 else 0.0
        offset = (group.index(idx) - (len(group) - 1) / 2.0) * spread
        deg = sec_idx * step + offset

        is_brass = ps["hand"] == "brass"
        hand_img = Image.open(ASSETS_DIR / f"hand_{ps['hand']}.png").convert("RGBA")
        pivot_frac = HAND_PIVOTS.get(ps["hand"], 0.75)
        target_h = int(round((355 if is_brass else 415) * scale))
        target_w = max(1, int(round(hand_img.width * (target_h / hand_img.height))))
        hand_resized = hand_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        hand_layer = Image.new("RGBA", (CLOCK_SIZE, CLOCK_SIZE), (0, 0, 0, 0))
        paste_x = int(round(cx - target_w / 2.0))
        paste_y = int(round(cy - target_h * pivot_frac))
        hand_layer.alpha_composite(hand_resized, (paste_x, paste_y))

        rot_hand = hand_layer.rotate(-deg, resample=Image.Resampling.BICUBIC, center=(cx, cy))

        h_shadow = Image.new("RGBA", (CLOCK_SIZE, CLOCK_SIZE), (0, 0, 0, 0))
        h_shadow.paste(Image.new("RGBA", (CLOCK_SIZE, CLOCK_SIZE), (20, 15, 10, 95)), (4, 6), rot_hand.getchannel("A"))
        h_shadow = h_shadow.filter(ImageFilter.GaussianBlur(4.0))
        canvas.alpha_composite(h_shadow, (0, 0))
        canvas.alpha_composite(rot_hand, (0, 0))

        med_dist = (135 if is_brass else 178) * scale
        rad = math.radians(deg)
        mx = cx + math.sin(rad) * med_dist
        my = cy - math.cos(rad) * med_dist
        med_diam = int(round(72 * scale))
        med = make_circle_avatar(ps["avatar"], med_diam, ps["color"], ring_width=4, initial=ps["name"][:1].upper())
        canvas.alpha_composite(med, (int(round(mx - med.width / 2)), int(round(my - med.height / 2))))

    # Layer 6: Center hub
    hub = Image.open(ASSETS_DIR / "center_hub.png").convert("RGBA")
    hub_sz = int(round(106 * scale))
    hub = hub.resize((hub_sz, hub_sz), Image.Resampling.LANCZOS)
    hub_sh = Image.new("RGBA", (hub_sz + 16, hub_sz + 16), (0, 0, 0, 0))
    hub_sh.paste(Image.new("RGBA", (hub_sz, hub_sz), (15, 12, 8, 110)), (10, 12), hub.getchannel("A"))
    hub_sh = hub_sh.filter(ImageFilter.GaussianBlur(4.0))
    canvas.alpha_composite(hub_sh, (int(round(cx - hub_sh.width / 2)), int(round(cy - hub_sh.height / 2))))
    canvas.alpha_composite(hub, (int(round(cx - hub_sz / 2)), int(round(cy - hub_sz / 2))))

    # Bottom status panel (only when portrait height leaves room below the clock)
    if PAGE_H - CLOCK_SIZE >= 240:
        draw = ImageDraw.Draw(canvas)
        panel_top = CLOCK_SIZE + 8
        margin_x = 64
        draw.line((margin_x, panel_top, PAGE_W - margin_x, panel_top), fill=(196, 186, 168, 255), width=2)

        shown = person_states[:2]
        card_gap = 28
        card_w = (PAGE_W - 2 * margin_x - card_gap * max(0, len(shown) - 1)) // max(1, len(shown))
        card_h = 204
        card_y = panel_top + 34

        name_font = load_font(33, bold=True)
        loc_font = load_font(29, bold=True)
        sub_font = load_font(21, bold=False)
        time_font = load_font(20, bold=False)

        for idx, ps in enumerate(shown):
            x0 = margin_x + idx * (card_w + card_gap)
            y0 = card_y
            x1 = x0 + card_w
            y1 = y0 + card_h

            draw.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=24,
                fill=(248, 244, 236, 255),
                outline=(210, 200, 182, 255),
                width=2,
            )

            av = make_circle_avatar(ps["avatar"], 92, ps["color"], ring_width=4, initial=ps["name"][:1].upper())
            canvas.alpha_composite(av, (x0 + 20, y0 + (card_h - av.height) // 2))
            draw = ImageDraw.Draw(canvas)

            tx = x0 + 20 + av.width + 16
            sec_name = SECTIONS[ps["sec_idx"]]["name"]
            raw = ps["raw_state"]
            sub_loc = ""
            if raw and raw.lower() != sec_name.lower() and raw not in ("home", "not_home") and "Virtueel" not in raw:
                sub_loc = raw if len(raw) <= 24 else raw[:23] + "…"
            since = format_since(ps["last_changed"])

            if sub_loc:
                draw.rounded_rectangle((tx, y0 + 32, tx + 26, y0 + 40), radius=4, fill=ps["color"] + (255,))
                draw.text((tx + 36, y0 + 18), ps["name"], font=name_font, fill=(28, 26, 23, 255))
                draw.text((tx, y0 + 64), sec_name, font=loc_font, fill=(154, 116, 46, 255))
                draw.text((tx, y0 + 104), sub_loc, font=sub_font, fill=(68, 62, 54, 255))
                if since:
                    draw.text((tx, y0 + 142), since, font=time_font, fill=(122, 114, 102, 255))
            else:
                draw.rounded_rectangle((tx, y0 + 46, tx + 26, y0 + 54), radius=4, fill=ps["color"] + (255,))
                draw.text((tx + 36, y0 + 32), ps["name"], font=name_font, fill=(28, 26, 23, 255))
                draw.text((tx, y0 + 82), sec_name, font=loc_font, fill=(154, 116, 46, 255))
                if since:
                    draw.text((tx, y0 + 128), since, font=time_font, fill=(122, 114, 102, 255))

        now = datetime.now()
        footer = f"{DAYS_NL[now.weekday()]} {now.day} {MONTHS_NL[now.month - 1]}  ·  bijgewerkt {now.strftime('%H:%M')}"
        footer_font = load_font(22, bold=False)
        fb = draw.textbbox((0, 0), footer, font=footer_font)
        fw = fb[2] - fb[0]
        draw.text(((PAGE_W - fw) / 2, PAGE_H - 74), footer, font=footer_font, fill=(115, 107, 94, 255))

    out_rgb = canvas.convert("RGB")
    if EINK_BOOST:
        out_rgb = ImageEnhance.Color(out_rgb).enhance(1.18)
        out_rgb = ImageEnhance.Contrast(out_rgb).enhance(1.06)

    buf = io.BytesIO()
    out_rgb.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def fetch_remote_image(url: str, use_ha_auth: bool = False, ttl: int = 3600) -> Optional[Image.Image]:
    if not url:
        return None
    full_url = url if url.startswith("http") else f"{HA_URL}{url}"
    now = time.time()
    cached = _media_img_cache.get(full_url)
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    headers = ha_headers() if use_ha_auth else {
        "User-Agent": "KoboFamilieklok/1.2 (https://github.com/DeRoelO/HA_kobo_weasly_clock; home-assistant-addon)"
    }
    try:
        r = requests.get(full_url, headers=headers, timeout=8)
        if r.ok:
            im = Image.open(io.BytesIO(r.content)).convert("RGBA")
            _media_img_cache[full_url] = (now, im)
            return im
    except Exception as exc:
        print(f"[Media] Failed to fetch image {full_url}: {exc}", file=sys.stderr)
    return None


def detect_active_media(demo_media: str = "") -> Optional[dict]:
    """Return active TV/Gaming descriptor or None when idle."""
    if demo_media:
        key = demo_media.lower()
        if key == "ps5":
            return {
                "kind": "gaming",
                "badge": "NU AAN HET GAMEN",
                "platform": "PlayStation 5",
                "title": "Demo Game",
                "image_url": "",
                "use_ha_auth": False,
                "sig": "demo:ps5",
            }
        label, logo_url = SERVICE_META.get(key, (demo_media.title(), ""))
        return {
            "kind": "service",
            "badge": "NU OP TV",
            "platform": label,
            "title": label,
            "image_url": logo_url,
            "use_ha_auth": False,
            "sig": f"demo:{key}",
        }

    if not MEDIA_ENABLED or not HA_URL or not HA_TOKEN:
        return None

    # 1. Active Jellyfin / movie poster
    if MEDIA_POSTER_ENTITY:
        st = fetch_ha_state(MEDIA_POSTER_ENTITY) or {}
        val = str(st.get("state", "")).strip()
        if val.lower() not in INACTIVE_STATES:
            attrs = st.get("attributes") or {}
            pic = attrs.get("entity_picture") or ""
            return {
                "kind": "poster",
                "badge": "NU OP TV",
                "platform": "Jellyfin",
                "title": str(attrs.get("media_title") or attrs.get("title") or ""),
                "image_url": pic,
                "use_ha_auth": True,
                "sig": f"poster:{val}:{pic}",
            }

    # 2. Active Gaming (e.g. PlayStation 5)
    if MEDIA_GAMING_SENSOR:
        st = fetch_ha_state(MEDIA_GAMING_SENSOR) or {}
        val = str(st.get("state", "")).strip()
        if val.lower() not in INACTIVE_STATES:
            pic = ""
            img_state = ""
            if MEDIA_GAMING_IMAGE:
                ist = fetch_ha_state(MEDIA_GAMING_IMAGE) or {}
                img_state = str(ist.get("state", ""))
                pic = (ist.get("attributes") or {}).get("entity_picture") or ""
            return {
                "kind": "gaming",
                "badge": "NU AAN HET GAMEN",
                "platform": "PlayStation 5",
                "title": val,
                "image_url": pic,
                "use_ha_auth": True,
                "sig": f"gaming:{val}:{img_state}",
            }

    # 3. Active Streaming Service
    if MEDIA_SERVICE_SENSOR:
        st = fetch_ha_state(MEDIA_SERVICE_SENSOR) or {}
        val = str(st.get("state", "")).strip()
        if val.lower() not in INACTIVE_STATES:
            key = val.lower()
            label, logo_url = SERVICE_META.get(key, (val.replace("_", " ").title(), ""))
            return {
                "kind": "service",
                "badge": "NU OP TV",
                "platform": label,
                "title": label,
                "image_url": logo_url,
                "use_ha_auth": False,
                "sig": f"service:{key}",
            }

    return None


def _round_corners(im: Image.Image, radius: int) -> Image.Image:
    im = im.convert("RGBA")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=radius, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int, max_lines: int = 2) -> List[str]:
    words = text.split()
    if not words:
        return []
    lines: List[str] = []
    cur = words[0]
    for w in words[1:]:
        test = f"{cur} {w}"
        if draw.textbbox((0, 0), test, font=font)[2] <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines - 1:
                break
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # Ellipsize last line if too wide
    while draw.textbbox((0, 0), lines[-1], font=font)[2] > max_w and len(lines[-1]) > 4:
        lines[-1] = lines[-1][:-2].rstrip() + "…"
    return lines


def render_media_png(media: dict) -> bytes:
    """Render a crisp 1072x1448 e-ink 'Nu op TV & Gaming' display."""
    kind = media["kind"]
    badge = media["badge"]
    platform = media["platform"]
    title = media["title"]

    # Warm charcoal cinema backdrop (high contrast, clean on Kaleido 3)
    canvas = Image.new("RGBA", (PAGE_W, PAGE_H), (24, 24, 28, 255))

    artwork = fetch_remote_image(
        media.get("image_url", ""),
        use_ha_auth=media.get("use_ha_auth", False),
        ttl=120 if media.get("use_ha_auth") else 86400,
    )

    # Subtle dark blurred ambient backdrop for posters/covers
    if artwork is not None and kind in ("poster", "gaming"):
        bg = artwork.convert("RGB")
        scale = max(PAGE_W / bg.width, PAGE_H / bg.height)
        bg = bg.resize((int(bg.width * scale), int(bg.height * scale)), Image.Resampling.LANCZOS)
        left = (bg.width - PAGE_W) // 2
        top = (bg.height - PAGE_H) // 2
        bg = bg.crop((left, top, left + PAGE_W, top + PAGE_H))
        bg = bg.filter(ImageFilter.GaussianBlur(34))
        bg = ImageEnhance.Brightness(bg).enhance(0.28)
        canvas.paste(bg.convert("RGBA"), (0, 0))

    draw = ImageDraw.Draw(canvas)

    # Top pill badge ("NU OP TV" / "NU AAN HET GAMEN")
    badge_font = load_font(28, bold=True)
    bb = draw.textbbox((0, 0), badge, font=badge_font)
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    pill_w = bw + 64
    pill_h = bh + 28
    px0 = (PAGE_W - pill_w) // 2
    py0 = 56
    pill_fill = (184, 146, 74, 255) if kind == "gaming" else (235, 228, 214, 255)
    pill_text = (20, 18, 16, 255)
    draw.rounded_rectangle((px0, py0, px0 + pill_w, py0 + pill_h), radius=pill_h // 2, fill=pill_fill)
    draw.text((px0 + 32 - bb[0], py0 + 14 - bb[1]), badge, font=badge_font, fill=pill_text)

    # Main artwork area
    area_top = py0 + pill_h + 44
    area_bottom = PAGE_H - 220
    area_h = area_bottom - area_top
    area_w = PAGE_W - 120

    if kind == "service":
        # Warm off-white gallery card so both dark & bright logos pop cleanly on e-ink
        card_w = PAGE_W - 120
        card_h = 560
        cx0 = (PAGE_W - card_w) // 2
        cy0 = area_top + (area_h - card_h) // 2
        draw.rounded_rectangle(
            (cx0, cy0, cx0 + card_w, cy0 + card_h),
            radius=40,
            fill=(248, 244, 236, 255),
            outline=(184, 146, 74, 255),
            width=4,
        )
        if artwork is not None:
            max_lw = int(card_w * 0.72)
            max_lh = int(card_h * 0.62)
            scale = min(max_lw / artwork.width, max_lh / artwork.height)
            lw = max(1, int(artwork.width * scale))
            lh = max(1, int(artwork.height * scale))
            logo_rs = artwork.resize((lw, lh), Image.Resampling.LANCZOS)
            canvas.alpha_composite(logo_rs, (cx0 + (card_w - lw) // 2, cy0 + (card_h - lh) // 2))
        else:
            big_font = load_font(86, bold=True)
            tb = draw.textbbox((0, 0), platform, font=big_font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            draw.text((cx0 + (card_w - tw) // 2 - tb[0], cy0 + (card_h - th) // 2 - tb[1]), platform, font=big_font, fill=(28, 26, 23, 255))
        draw = ImageDraw.Draw(canvas)
    elif artwork is not None:
        scale = min(area_w / artwork.width, area_h / artwork.height)
        aw = max(1, int(artwork.width * scale))
        ah = max(1, int(artwork.height * scale))
        art_rs = _round_corners(artwork.resize((aw, ah), Image.Resampling.LANCZOS), radius=32)
        ax0 = (PAGE_W - aw) // 2
        ay0 = area_top + (area_h - ah) // 2
        # Soft drop shadow
        sh = Image.new("RGBA", (aw + 36, ah + 36), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((18, 18, aw + 18, ah + 18), radius=32, fill=(0, 0, 0, 170))
        sh = sh.filter(ImageFilter.GaussianBlur(14))
        canvas.alpha_composite(sh, (ax0 - 18, ay0 - 12))
        canvas.alpha_composite(art_rs, (ax0, ay0))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((ax0, ay0, ax0 + aw, ay0 + ah), radius=32, outline=(255, 255, 255, 65), width=3)
    else:
        # Fallback card if cover couldn't be fetched
        card_w = PAGE_W - 140
        card_h = 460
        cx0 = (PAGE_W - card_w) // 2
        cy0 = area_top + (area_h - card_h) // 2
        draw.rounded_rectangle((cx0, cy0, cx0 + card_w, cy0 + card_h), radius=36, fill=(40, 40, 48, 255), outline=(184, 146, 74, 255), width=3)
        big_font = load_font(64, bold=True)
        tb = draw.textbbox((0, 0), platform, font=big_font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((cx0 + (card_w - tw) // 2 - tb[0], cy0 + (card_h - th) // 2 - tb[1]), platform, font=big_font, fill=(242, 236, 224, 255))

    # Bottom title + platform / date footer
    display_title = title if (title and title.lower() != platform.lower()) else platform
    title_font = load_font(42, bold=True)
    lines = _wrap_text(draw, display_title, title_font, PAGE_W - 120, max_lines=2)
    ty = area_bottom + 24
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=title_font)
        lw = lb[2] - lb[0]
        draw.text(((PAGE_W - lw) / 2 - lb[0], ty), line, font=title_font, fill=(245, 240, 230, 255))
        ty += 52

    now = datetime.now()
    sub = f"{platform}  ·  {DAYS_NL[now.weekday()]} {now.day} {MONTHS_NL[now.month - 1]}"
    sub_font = load_font(24, bold=False)
    sb = draw.textbbox((0, 0), sub, font=sub_font)
    sw = sb[2] - sb[0]
    draw.text(((PAGE_W - sw) / 2 - sb[0], PAGE_H - 72), sub, font=sub_font, fill=(175, 168, 155, 255))

    out_rgb = canvas.convert("RGB")
    if EINK_BOOST:
        out_rgb = ImageEnhance.Color(out_rgb).enhance(1.20)
        out_rgb = ImageEnhance.Contrast(out_rgb).enhance(1.06)

    buf = io.BytesIO()
    out_rgb.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def get_cached_png(demo_mode: bool = False, force_clock: bool = False, demo_media: str = "") -> bytes:
    global _render_cache
    now = time.time()
    media = None if force_clock else detect_active_media(demo_media=demo_media)
    cache_key = f"media:{media['sig']}" if media else ("demo_clock" if demo_mode else "clock")

    if not demo_mode and not demo_media and _render_cache[2] and _render_cache[1] == cache_key and (now - _render_cache[0]) < CACHE_SECONDS:
        return _render_cache[2]

    png = render_media_png(media) if media is not None else render_clock_png(demo_mode=demo_mode)
    if not demo_mode and not demo_media:
        _render_cache = (now, cache_key, png)
    return png


def get_state_signature(demo_mode: bool = False) -> str:
    """Short deterministic hash of all tracked person states + active TV/Gaming + current date."""
    parts = [datetime.now().strftime("%Y-%m-%d")]
    if demo_mode or not HA_URL or not HA_TOKEN:
        parts.append("demo")
    else:
        media = detect_active_media()
        if media is not None:
            parts.append(f"MEDIA:{media['sig']}")
        else:
            parts.append("MEDIA:idle")
            for p in PERSONS:
                st = fetch_ha_state(p["entity"]) or {}
                parts.append(f"{p['entity']}:{st.get('state', '')}:{st.get('last_changed', '')}")
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12]


class KoboKlokHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        global _render_cache
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        demo = qs.get("demo", ["0"])[0] == "1"

        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        if parsed.path == "/state":
            sig = get_state_signature(demo_mode=demo)
            body = f"{sig}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/wait":
            since = qs.get("since", [""])[0].strip()
            try:
                timeout = int(qs.get("timeout", ["55"])[0])
            except ValueError:
                timeout = 55
            timeout = max(5, min(120, timeout))

            deadline = time.time() + timeout
            sig = get_state_signature(demo_mode=demo)
            while sig == since and time.time() < deadline:
                time.sleep(3)
                sig = get_state_signature(demo_mode=demo)

            changed = sig != since
            if changed:
                _render_cache = (0.0, "", b"")
            status_word = "CHANGED" if changed else "UNCHANGED"
            body = f"{status_word} {sig}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/klok.png":
            force_clock = qs.get("mode", [""])[0].lower() == "clock"
            demo_media = qs.get("demo_media", [""])[0].strip()
            png = get_cached_png(demo_mode=demo, force_clock=force_clock, demo_media=demo_media)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
            return

        ts = int(time.time())
        demo_q = "&demo=1" if demo else ""
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta http-equiv="refresh" content="{REFRESH_SECONDS}">
  <title>Familieklok</title>
  <style>
    html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; background: #ebe4d6; overflow: hidden; }}
    a {{ display: block; width: 100%; height: 100%; }}
    img {{ display: block; width: 100%; height: 100%; object-fit: contain; }}
  </style>
</head>
<body>
  <a href="/?t={ts}{demo_q}"><img src="/klok.png?t={ts}{demo_q}" alt="Familieklok"></a>
</body>
</html>""".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--render-demo":
        out_path = Path(sys.argv[2])
        out_path.write_bytes(render_clock_png(demo_mode=True))
        print(f"Rendered demo PNG to {out_path} ({out_path.stat().st_size} bytes)")
        sys.exit(0)

    print(f"Kobo Klok server running on http://0.0.0.0:{PORT} (HA_URL={HA_URL or 'DEMO MODE'})")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), KoboKlokHandler)
    server.serve_forever()
