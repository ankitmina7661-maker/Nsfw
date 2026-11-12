import base64
import json
import logging
import os
import re
import threading
import unicodedata
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # logs me bot token na dikhe
log = logging.getLogger("modbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")  # optional, free moderation API
IMG_THRESHOLD = float(os.environ.get("IMG_THRESHOLD", "0.6"))
TXT_THRESHOLD = float(os.environ.get("TXT_THRESHOLD", "0.8"))

# ---------- word list ----------
with open("words.txt", encoding="utf-8") as f:
    WORDS = [w.strip().lower() for w in f if w.strip() and not w.startswith("#")]

LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return text.translate(LEET)


def squash(text: str) -> str:
    # "p o r n" / "p.o.r.n" / "poorrnn" -> "porn"
    t = re.sub(r"[^a-z]", "", normalize(text))
    return re.sub(r"(.)\1+", r"\1", t)


# short words match only as whole words (avoid false positives), long ones also match squashed
SHORT = {w for w in WORDS if len(w) <= 4}
LONG = [re.sub(r"(.)\1+", r"\1", w) for w in WORDS if len(w) > 4]


def bad_text(text: str) -> bool:
    n = normalize(text)
    tokens = set(re.findall(r"[a-z]+", n))
    if tokens & SHORT:
        return True
    sq = squash(text)
    return any(w in sq for w in LONG)


# ---------- caches ----------
class LRU(OrderedDict):
    def __init__(self, size=2000):
        super().__init__()
        self.size = size

    def put(self, k, v):
        self[k] = v
        self.move_to_end(k)
        if len(self) > self.size:
            self.popitem(last=False)


verdicts = LRU()  # sticker file_unique_id -> bool
BANNED_FILE = "banned_sets.json"
try:
    banned_sets = set(json.load(open(BANNED_FILE)))
except Exception:
    banned_sets = set()


# ---------- OpenAI free moderation (optional) ----------
http = httpx.AsyncClient(timeout=20)


async def openai_moderate(inp) -> dict | None:
    if not OPENAI_KEY:
        return None
    try:
        r = await http.post(
            "https://api.openai.com/v1/moderations",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": "omni-moderation-latest", "input": inp},
        )
        r.raise_for_status()
        return r.json()["results"][0]["category_scores"]
    except Exception as e:
        log.warning("moderation api failed: %s", e)
        return None


async def bad_image(bot, sticker) -> bool:
    if sticker.file_unique_id in verdicts:
        return verdicts[sticker.file_unique_id]
    result = False
    # animated (.tgs) / video (.webm) stickers: use the static thumbnail
    if sticker.is_animated or sticker.is_video:
        target = sticker.thumbnail
    else:
        target = sticker
    if target is not None:
        tg_file = await bot.get_file(target.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        mime = "image/webp" if (tg_file.file_path or "").endswith(".webp") else "image/jpeg"
        b64 = base64.b64encode(data).decode()
        scores = await openai_moderate(
            [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
        )
        if scores:
            result = scores.get("sexual", 0) >= IMG_THRESHOLD
    verdicts.put(sticker.file_unique_id, result)
    return result


# ---------- handlers ----------
async def delete(update: Update, why: str):
    m = update.effective_message
    try:
        await m.delete()
        log.info("deleted msg %s in chat %s (%s)", m.message_id, m.chat_id, why)
    except Exception as e:
        log.warning("delete failed (bot admin + 'Delete messages' permission chahiye): %s", e)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    text = m.text or m.caption or ""
    if not text:
        return
    if bad_text(text):
        return await delete(update, "keyword")
    if OPENAI_KEY and len(text) > 15:
        scores = await openai_moderate(text)
        if scores and scores.get("sexual", 0) >= TXT_THRESHOLD:
            await delete(update, "text-ai")


async def on_sticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = update.effective_message.sticker
    if st.set_name and (st.set_name in banned_sets or bad_text(st.set_name.replace("_", " "))):
        if st.set_name not in banned_sets:
            banned_sets.add(st.set_name)
            json.dump(sorted(banned_sets), open(BANNED_FILE, "w"))
        return await delete(update, "sticker-set")
    if await bad_image(ctx.bot, st):
        # sirf yahi sticker delete hoga, pack ban nahi hoga
        # (dobara aaya to cache se turant delete, API call nahi lagegi)
        await delete(update, "sticker-image")


# ---------- tiny health server (Koyeb/Render jaise hosts ke liye) ----------
def health_server():
    port = os.environ.get("PORT")
    if not port:
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()


def main():
    health_server()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    grp = filters.ChatType.GROUPS
    app.add_handler(MessageHandler(grp & filters.Sticker.ALL, on_sticker))
    app.add_handler(MessageHandler(grp & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=["message", "edited_message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
