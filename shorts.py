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
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

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
    bg = ImageOps.fit(img, (W, H), method=Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    canvas = ImageEnhance.Brightness(bg).enhance(0.45)
    d = ImageDraw.Draw(canvas)

    # заголовок
    size = 84
    while True:
        f = ImageFont.truetype(FONT_BOLD, size)
        lines = wrap(d, title, f, W - 120)
        if len(lines) <= 3 or size <= 56:
            break
        size -= 8
    y = 110
    for ln in lines[:3]:
        d.text((60, y), ln, font=f, fill="white")
        y += int(size * 1.2)

    # фото со скруглёнными углами
    ph = img.copy()
    ph.thumbnail((960, 820), Image.LANCZOS)
    fb = ImageFont.truetype(FONT, 44)
    blocks = [wrap(d, "• " + b, fb, W - 140)[:2] for b in bullets[:3]]
    bullets_h = sum(len(lines) * 56 + 14 for lines in blocks)
    free_top = max(y + 30, 380)
    free_h = (H - 230) - free_top
    offset = max(0, (free_h - (ph.size[1] + 40 + bullets_h)) // 2)
    top = free_top + offset
    mask = Image.new("L", ph.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ph.size[0], ph.size[1]), radius=36, fill=255)
    canvas.paste(ph, ((W - ph.size[0]) // 2, top), mask)
    y = top + ph.size[1] + 40

    # пункты
    for lines in blocks:
        for ln in lines:
            d.text((70, y), ln, font=fb, fill="white")
            y += 56
        y += 14

    # призыв в Telegram
    d.rounded_rectangle((60, H - 200, W - 60, H - 70), radius=40, fill=(255, 196, 0))
    cta = f"Скачать в Telegram: {handle}" if handle else "Скачать бесплатно в Telegram"
    fs = 50
    while fs > 30:
        fc = ImageFont.truetype(FONT_BOLD, fs)
        if d.textlength(cta, font=fc) <= W - 160:
            break
        fs -= 4
    d.text((W // 2, H - 160), cta, font=fc, fill="black", anchor="mm")
    d.text((W // 2, H - 105), "ссылка в шапке канала", font=ImageFont.truetype(FONT, 32),
           fill="black", anchor="mm")
    canvas.save(out_png)


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
