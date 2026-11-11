import os
import tempfile

# Model cache ke liye writable folder (home read-only hai)
CACHE = "/data/cache"
try:
    os.makedirs(CACHE, exist_ok=True)
except OSError:
    CACHE = tempfile.gettempdir()
os.environ["HOME"] = CACHE
os.environ["XDG_CACHE_HOME"] = CACHE

import re
import io
import asyncio
import logging

from PIL import Image
import nsfwpy
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ["BOT_TOKEN"]

# Sakhti yahan se set karo (kam value = zyada sakht)
PORN_LIMIT = 0.50   # Porn + Hentai ka total
SEXY_LIMIT = 0.80   # Sexy category
MAX_BYTES = 5_000_000

BAD_WORDS = [
    # apne group ke hisab se words add karo
    "porn", "xxx", "nude", "sex video", "nsfw", "onlyfans",
    "चोदना", "लंड", "चूत",
]
BAD_PATTERN = re.compile(
    "|".join(re.escape(w) for w in BAD_WORDS), re.IGNORECASE
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("modbot")

model = nsfwpy.load_model("mobilenet_v2")
lock = asyncio.Semaphore(1)  # ek baar me ek hi image, RAM bachane ke liye


def is_nsfw_image(data: bytes) -> bool:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((512, 512))
    res = model.classify(img, top_k=5)
    p = {r["className"]: r["probability"] for r in res}
    bad = p.get("Porn", 0) + p.get("Hentai", 0)
    sexy = p.get("Sexy", 0)
    return bad >= PORN_LIMIT or sexy >= SEXY_LIMIT


def pick_file(msg):
    if msg.photo:
        return msg.photo[min(2, len(msg.photo) - 1)]
    st = msg.sticker
    if st:
        if st.is_video or st.is_animated:
            return st.thumbnail
        return st
    for m in (msg.animation, msg.video, msg.video_note):
        if m:
            return m.thumbnail
    d = msg.document
    if d and (d.mime_type or "").startswith("image/"):
        return d
    return None


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat.type not in ("group", "supergroup"):
        return
    try:
        text = msg.text or msg.caption or ""
        if text and BAD_PATTERN.search(text):
            await msg.delete()
            return

        f = pick_file(msg)
        if not f or (getattr(f, "file_size", None) or 0) > MAX_BYTES:
            return

        tg_file = await context.bot.get_file(f.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        async with lock:
            bad = await asyncio.to_thread(is_nsfw_image, data)
        if bad:
            await msg.delete()
            log.info("Deleted NSFW media")
    except Exception as e:
        log.warning("Error: %s", e)


def main():
    app = Application.builder().token(TOKEN).concurrent_updates(2).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
