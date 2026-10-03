import os, re, json, glob, random, shutil, subprocess, asyncio, time, requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.environ["TG_SESSION"]
TARGET = os.environ["TARGET_CHANNEL"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
HANDLE = os.environ.get("CHANNEL_HANDLE", "").strip() or (TARGET if TARGET.startswith("@") else "")
SHORTS_CHAT = os.environ.get("SHORTS_CHAT", "").strip() or "me"   # "me" = «Избранное»
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
SCAN = 300          # сколько последних сообщений своего канала просматривать
PER_RUN = 1         # сколько роликов делать за запуск
MAX_PHOTOS = 5      # максимум фото в одном ролике (слайдшоу)
MAX_FAILS = 3
STATE = "shorts_state.json"
WORK = "work"
W, H, FPS = 1080, 1920, 25
# --- оформление карточки ---
TITLE_FONT = "Montserrat:bold"      # шрифт заголовка: «семейство:начертание»
TEXT_FONT = "Open Sans:semibold"    # шрифт пунктов
CTA_FONT = "Montserrat:bold"        # шрифт плашки с призывом
ACCENT = (255, 196, 0)              # фирменный цвет (R, G, B)
FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Свой шрифт: положи файлы fonts/title.ttf и fonts/text.ttf в репозиторий, и они заменят шрифты выше.
SAFE_TOP = 190       # сверху YouTube закрывает интерфейсом
SAFE_BOTTOM = 360    # снизу закрывают название, описание и музыка
COLUMN_W = 880       # ширина центральной колонки

PROMPT = (
    "Ты редактор канала про Minecraft Bedrock. По тексту поста сделай текст для вертикального "
    "ролика (YouTube Shorts). Используй только факты из текста, ничего не выдумывай. "
    "Верни ТОЛЬКО JSON без пояснений в таком виде:\n"
    '{"title": "цепляющий заголовок до 45 символов, без эмодзи и хэштегов",\n'
    ' "bullets": ["три коротких пункта до 55 символов: что делает мод или что на фото, ключевые механики"],\n'
    ' "yt_title": "название видео для YouTube до 90 символов, с упоминанием Minecraft",\n'
    ' "yt_description": "2-3 предложения о моде или идее и призыв скачать бесплатно в Telegram, '
    'ссылка в шапке канала; в конце хэштеги #Shorts #Minecraft и ещё 2-3 по теме"}\n\n'
    "Текст поста:\n"
)


# ---------- Gemini ----------

def gemini_json(text):
    order = MODELS[:]
    random.shuffle(order)
    for model in order:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in (1, 2):
            try:
                r = requests.post(
                    url,
                    headers={"x-goog-api-key": GEMINI_KEY},
                    json={
                        "contents": [{"parts": [{"text": PROMPT + text}]}],
                        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
                    },
                    timeout=60,
                )
            except Exception as e:
                print(f"  Gemini {model}: сетевая ошибка ({e}), попытка {attempt}/2")
                time.sleep(3)
                continue
            if r.status_code == 429:
                print(f"  Gemini {model}: лимит исчерпан (429)")
                break
            if r.status_code in (500, 502, 503, 504):
                print(f"  Gemini {model}: HTTP {r.status_code}, попытка {attempt}/2")
                time.sleep(5)
                continue
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("title") and data.get("bullets"):
                    print(f"  Gemini {model}: OK")
                    return data
            except Exception:
                pass
            print(f"  Gemini {model}: неверный ответ: {r.text[:150]}")
            break
        print("  переключаюсь на следующую модель")
    return None


# ---------- карточка ----------

_font_cache = {}


def get_font(kind, size):
    spec, custom = {
        "title": (TITLE_FONT, "fonts/title.ttf"),
        "text": (TEXT_FONT, "fonts/text.ttf"),
        "cta": (CTA_FONT, "fonts/title.ttf"),
    }[kind]
    if os.path.exists(custom):
        path = custom
    else:
        if spec not in _font_cache:
            try:
                out = subprocess.run(["fc-match", "-f", "%{file}", spec],
                                     capture_output=True, text=True).stdout.strip()
            except Exception:
                out = ""
            _font_cache[spec] = out if out and os.path.exists(out) else FALLBACK_FONT
        path = _font_cache[spec]
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.truetype(FALLBACK_FONT, size)


def clean(s):
    s = re.sub(r"https?://\S+|@\w+", "", s or "")
    s = "".join(ch for ch in s if ch.isalnum() or ch in " .,:;!?-—–+()/%'\"«»№&*•…")
    return re.sub(r"\s+", " ", s).strip()


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_card(photo_path, title, bullets, handle, out_png):
    img = Image.open(photo_path).convert("RGB")
    bg = ImageOps.fit(img, (W, H), method=Image.LANCZOS).filter(ImageFilter.GaussianBlur(45))
    canvas = ImageEnhance.Brightness(bg).enhance(0.40).convert("RGBA")
    measure = ImageDraw.Draw(canvas)

    CW = COLUMN_W
    X0 = (W - CW) // 2
    PAD, GAP = 34, 34

    # --- заголовок
    size = 74
    while True:
        ft = get_font("title", size)
        t_lines = wrap(measure, title, ft, CW - 2 * PAD)
        if len(t_lines) <= 3 or size <= 48:
            break
        size -= 6
    t_lines = t_lines[:3]
    lh_t = int(size * 1.22)
    title_h = len(t_lines) * lh_t + 2 * PAD

    # --- пункты (каждый на своей плашке)
    fb = get_font("text", 40)
    b_lines = [wrap(measure, b, fb, CW - 2 * PAD)[:2] for b in bullets[:3]]
    lh_b, pad_b, gap_b = 52, 26, 18
    b_heights = [len(ls) * lh_b + 2 * pad_b for ls in b_lines]
    bullets_h = sum(b_heights) + gap_b * max(0, len(b_heights) - 1)

    # --- плашка с призывом
    cta_h = 130
    n_gaps = 3 if b_heights else 2
    fixed = title_h + bullets_h + cta_h + n_gaps * GAP

    # --- фото занимает всё оставшееся место
    avail = (H - SAFE_BOTTOM) - SAFE_TOP
    ph = img.copy()
    ph.thumbnail((CW - 24, max(300, min(780, avail - fixed - 16))), Image.LANCZOS)
    photo_h = ph.size[1] + 16
    stack_h = fixed + photo_h
    y = SAFE_TOP + max(0, (avail - stack_h) // 2)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    dark = (15, 15, 25, 215)

    # плашка заголовка
    title_y = y
    od.rounded_rectangle((X0, y, X0 + CW, y + title_h), radius=36, fill=dark,
                         outline=ACCENT + (255,), width=5)
    y += title_h + GAP

    # рамка под фото
    photo_y = y
    px = (W - ph.size[0]) // 2
    od.rounded_rectangle((px - 8, y, px + ph.size[0] + 8, y + photo_h), radius=40,
                         fill=(255, 255, 255, 235))
    y += photo_h + GAP

    # плашки пунктов
    bullet_ys = []
    for h in b_heights:
        od.rounded_rectangle((X0, y, X0 + CW, y + h), radius=30, fill=(15, 15, 25, 205),
                             outline=(255, 255, 255, 110), width=3)
        bullet_ys.append(y)
        y += h + gap_b
    y += GAP - gap_b if b_heights else 0

    # плашка призыва
    cta_y = y
    od.rounded_rectangle((X0, y, X0 + CW, y + cta_h), radius=40, fill=ACCENT + (255,))

    canvas = Image.alpha_composite(canvas, overlay)

    # фото со скруглёнными углами
    mask = Image.new("L", ph.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ph.size[0], ph.size[1]), radius=32, fill=255)
    canvas.paste(ph, (px, photo_y + 8), mask)

    # --- текст (всё по центру)
    d = ImageDraw.Draw(canvas)
    for i, ln in enumerate(t_lines):
        d.text((W // 2, title_y + PAD + i * lh_t + lh_t // 2), ln, font=ft, fill="white", anchor="mm")
    for by, ls in zip(bullet_ys, b_lines):
        for i, ln in enumerate(ls):
            d.text((W // 2, by + pad_b + i * lh_b + lh_b // 2), ln, font=fb, fill="white", anchor="mm")

    cta = f"Скачать в Telegram: {handle}" if handle else "Скачать бесплатно в Telegram"
    fs = 50
    while True:
        fc = get_font("cta", fs)
        if d.textlength(cta, font=fc) <= CW - 2 * PAD:
            break
        if fs <= 28:
            if cta != handle and handle:   # слишком длинно: оставляем только имя канала
                cta, fs = handle, 50
                continue
            break
        fs -= 4
    d.text((W // 2, cta_y + 52), cta, font=fc, fill="black", anchor="mm")
    d.text((W // 2, cta_y + 100), "ссылка в шапке канала", font=get_font("text", 30),
           fill=(40, 40, 40), anchor="mm")
    canvas.convert("RGB").save(out_png)


# ---------- видео ----------

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise RuntimeError("ffmpeg завершился с ошибкой")


def make_video(pngs, out):
    per = 12 if len(pngs) == 1 else 5
    clips = []
    for i, png in enumerate(pngs):
        clip = f"{WORK}/clip{i}.mp4"
        vf = (f"scale=2160:3840,zoompan=z='min(zoom+0.0006,1.12)':x='iw/2-(iw/zoom/2)'"
              f":y='ih/2-(ih/zoom/2)':d={per * FPS}:s={W}x{H}:fps={FPS},format=yuv420p")
        run(["ffmpeg", "-y", "-i", png, "-vf", vf, "-t", str(per),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", clip])
        clips.append(clip)
    with open(f"{WORK}/list.txt", "w") as fh:
        for c in clips:
            fh.write(f"file '{os.path.abspath(c)}'\n")
    silent = f"{WORK}/silent.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{WORK}/list.txt", "-c", "copy", silent])
    total = per * len(pngs)

    tracks = [t for ext in ("mp3", "m4a", "ogg", "wav") for t in glob.glob(f"music/*.{ext}")]
    if tracks:
        track = random.choice(tracks)
        print("  музыка:", os.path.basename(track))
        run(["ffmpeg", "-y", "-i", silent, "-stream_loop", "-1", "-i", track, "-t", str(total),
             "-filter_complex", f"[1:a]afade=t=in:st=0:d=1,afade=t=out:st={total - 1.5}:d=1.5[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", out])
    else:
        print("  папка music/ пуста, видео без музыки")
        shutil.copy(silent, out)
    return total


# ---------- поиск постов ----------

async def find_candidates(client, target, used):
    msgs = [m async for m in client.iter_messages(target, limit=SCAN)]
    msgs.reverse()  # от старых к новым
    posts, cur = [], None
    for m in msgs:
        if m.action:
            continue
        if m.photo:
            if cur and m.grouped_id and cur["gid"] == m.grouped_id:
                cur["photos"].append(m)
                if not cur["text"] and m.message:
                    cur["text"] = m.message
            else:
                cur = {"id": m.id, "gid": m.grouped_id, "text": m.message or "", "photos": [m]}
                posts.append(cur)
        else:
            cur = None
    return [p for p in posts if p["id"] not in used and len(p["text"]) >= 20]


async def main():
    os.makedirs(WORK, exist_ok=True)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    used = set(state.get("used", []))
    fails = state.get("fails", {})
    done = 0
    try:
        async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
            target = await client.get_entity(TARGET)
            cands = await find_candidates(client, target, used)
            print("Кандидатов:", len(cands))
            for p in reversed(cands):  # самые свежие первыми
                if done >= PER_RUN:
                    break
                tag = f"[пост #{p['id']}]"
                try:
                    print(tag, "скачиваю фото:", len(p["photos"][:MAX_PHOTOS]))
                    paths = []
                    for i, m in enumerate(p["photos"][:MAX_PHOTOS]):
                        path = await client.download_media(m, file=f"{WORK}/p{p['id']}_{i}.jpg")
                        if path:
                            paths.append(path)
                    if not paths:
                        raise RuntimeError("не удалось скачать фото")

                    print(tag, "запрос к Gemini")
                    data = gemini_json(p["text"])
                    if not data:
                        raise RuntimeError("Gemini не вернул текст")
                    title = clean(data["title"])[:60] or "Minecraft"
                    bullets = [clean(b)[:70] for b in data["bullets"] if clean(b)][:3]
                    yt_title = (data.get("yt_title") or title).strip()[:95]
                    yt_desc = (data.get("yt_description") or "").strip()
                    if "#shorts" not in yt_desc.lower():
                        yt_desc += "\n#Shorts #Minecraft"

                    pngs = []
                    for i, path in enumerate(paths):
                        png = f"{WORK}/card{i}.png"
                        render_card(path, title, bullets, HANDLE, png)
                        pngs.append(png)
                    out = f"{WORK}/short_{p['id']}.mp4"
                    total = make_video(pngs, out)

                    caption = f"НАЗВАНИЕ:\n{yt_title}\n\nОПИСАНИЕ:\n{yt_desc}"[:1024]
                    await client.send_file(
                        SHORTS_CHAT, out, caption=caption,
                        attributes=[DocumentAttributeVideo(duration=int(total), w=W, h=H,
                                                           supports_streaming=True)],
                    )
                    used.add(p["id"])
                    fails.pop(str(p["id"]), None)
                    done += 1
                    print(tag, "ролик отправлен в", SHORTS_CHAT)
                except Exception as e:
                    k = str(p["id"])
                    fails[k] = fails.get(k, 0) + 1
                    print(tag, f"ошибка: {e} (попытка {fails[k]}/{MAX_FAILS})")
                    if fails[k] >= MAX_FAILS:
                        used.add(p["id"])
                        print(tag, "пропущен навсегда")
    finally:
        state["used"] = sorted(used)[-2000:]
        state["fails"] = fails
        json.dump(state, open(STATE, "w"))
    print("Готово, роликов:", done)


asyncio.run(main())
