import os
import asyncio
import re
import requests
from yt_dlp import YoutubeDL
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv  # <-- добавили

# Загружаем переменные из .env
load_dotenv()

# Optional envs for sites that often require auth/cookies (e.g., Pinterest, some IG/FB links):
# YTDLP_BROWSER_COOKIES=safari   # or chrome / brave / chromium
# YTDLP_COOKIE_FILE=/path/to/cookies.txt  # Netscape format

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def _info_has_video(info: dict) -> bool:
    """
    Returns True if yt-dlp infodict indicates that a video stream exists.
    Checks both single-format and merged requested_formats cases.
    """
    # Case 1: direct fields
    if info.get("vcodec") and info.get("vcodec") != "none":
        return True
    # Case 2: requested_formats (list with audio/video parts)
    rf = info.get("requested_formats")
    if isinstance(rf, list):
        for part in rf:
            if part and part.get("vcodec") and part.get("vcodec") != "none":
                return True
    return False


def build_ydl_opts():
    """
    Build yt-dlp options to prefer compatible MP4 (H.264 + AAC) to avoid black video / audio-only issues.
    Transcodes when требуется (ffmpeg) в H.264/AAC.
    Supports optional cookies from browser via env var YTDLP_BROWSER_COOKIES (e.g., 'safari' or 'chrome').
    """
    opts = {
        # 1) Пытаемся взять связку video(H.264, mp4) + audio(m4a). Если нет — берём лучший mp4, иначе вообще best.
        'format': 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best',
        'outtmpl': '/tmp/%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        # финальный контейнер
        'merge_output_format': 'mp4',
        # насильно приводим кодек к H.264 + AAC, чтобы Telegram корректно играл
        'postprocessors': [
            {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'},
        ],
        # кодеки/параметры ffmpeg для совместимости
        'postprocessor_args': [
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k'
        ],
        # иногда параллельные фрагменты у некоторых сайтов (особенно IG/FB) дают сбои
        'concurrent_fragment_downloads': 1,
    }

    # Включаем куки из браузера при необходимости (например, для Pinterest, закрытых IG/FB)
    browser = os.getenv("YTDLP_BROWSER_COOKIES", "").strip().lower()
    if browser in ('safari', 'chrome', 'chromium', 'brave'):
        # Для macOS чаще всего подходит 'safari'
        # Для Chrome/Chromium/Brave можно также указать профиль, но по умолчанию возьмётся основной.
        opts['cookiesfrombrowser'] = (browser,)
    cookiefile = os.getenv("YTDLP_COOKIE_FILE", "").strip()
    if cookiefile:
        opts['cookiefile'] = cookiefile

    return opts

def pinterest_resolve_direct_media(page_url: str) -> str | None:
    """
    Tries to fetch the Pinterest page and extract a direct video URL from OpenGraph tags.
    Returns direct media URL (mp4/m3u8) or None.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Referer": "https://www.pinterest.com/",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(page_url, headers=headers, timeout=15)
        if r.status_code != 200 or not r.text:
            return None
        html = r.text
        # Common OG tags on Pinterest pages:
        # <meta property="og:video" content="https://v1.pinimg.com/videos/...mp4">
        # <meta property="og:video:secure_url" content="...">
        # <meta property="og:video:url" content="...">
        m = re.search(r'property=["\']og:video(?::secure_url|:url)?["\'][^>]*content=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
        if m:
            return m.group(1)
        # Some pages preload a video via <link rel="preload" as="video" href="...">
        m2 = re.search(r'<link[^>]+rel=["\']preload["\'][^>]+as=["\']video["\'][^>]+href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
        if m2:
            return m2.group(1)
        return None
    except Exception:
        return None

YDL_OPTS = build_ydl_opts()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришлите ссылку на видео/аудио (YouTube, TikTok, Instagram, Facebook, X, Pinterest и т.д.).")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ("Отправьте ссылку на YouTube/TikTok/Instagram/Facebook/X/Pinterest.\n"
           "Если Pinterest требует вход: задайте переменные окружения\n"
           "YTDLP_BROWSER_COOKIES=safari (или chrome/brave/chromium)\n"
           "или YTDLP_COOKIE_FILE=/path/to/cookies.txt\n")
    await update.message.reply_text(txt)

async def download_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    msg = await update.message.reply_text("Скачиваю... проверьте сообщение через минуту.")
    loop = asyncio.get_event_loop()

    def ytdl_download(u):
        # Pinterest: попробуем заранее вытащить прямую ссылку из OG-тегов.
        pin_direct = None
        if "pinterest." in u:
            pin_direct = pinterest_resolve_direct_media(u)

        # Попытка №1: наши базовые опции (mp4+h264+aac)
        opts1 = build_ydl_opts()
        if "pinterest." in u:
            # Добавляем заголовки для Pinterest (часто требуется Referer/User-Agent)
            opts1 = {**opts1, 'http_headers': {'Referer': 'https://www.pinterest.com/',
                                               'User-Agent': 'Mozilla/5.0'}}
        target_url = pin_direct or u
        with YoutubeDL(opts1) as ydl:
            info = ydl.extract_info(target_url, download=True)
            filename = ydl.prepare_filename(info)
            if _info_has_video(info):
                return filename, info

        # Если получили только аудио (например, YouTube выдал только m4a или сайт отдал аудиодорожку),
        # делаем попытку №2 с более совместимыми прогрессивными вариантами (itag 22/18 для YouTube).
        opts2 = build_ydl_opts()
        opts2['format'] = 'best[ext=mp4]/22/18/best'
        if "pinterest." in u:
            opts2 = {**opts2, 'http_headers': {'Referer': 'https://www.pinterest.com/',
                                               'User-Agent': 'Mozilla/5.0'}}
        with YoutubeDL(opts2) as ydl2:
            info2 = ydl2.extract_info(target_url, download=True)
            filename2 = ydl2.prepare_filename(info2)
            return filename2, info2

    try:
        filename, info = await loop.run_in_executor(None, ytdl_download, url)
    except Exception as e:
        await msg.edit_text(f"Ошибка при скачивании: {e}")
        return

    # Страховка: если расширение неожиданно не mp4 — переупакуем (без полного перекодирования)
    if not filename.lower().endswith('.mp4'):
        try:
            tmp_mp4 = filename.rsplit('.', 1)[0] + '.mp4'
            os.system(f'ffmpeg -y -i "{filename}" -c:v libx264 -preset veryfast -crf 23 -c:a aac -b:a 192k "{tmp_mp4}"')
            if os.path.exists(tmp_mp4):
                try:
                    os.remove(filename)
                except:
                    pass
                filename = tmp_mp4
        except Exception:
            pass

    # Проверка размера файла (Telegram ограничение: 50 MB для ботов без special api; но можно использовать upload:methods)
    try:
        size = os.path.getsize(filename)
        # если файл большой, можно отправить как документ
        if size > 49 * 1024 * 1024:
            await msg.edit_text("Файл большой — отправляю как документ (может занять время).")
            with open(filename, "rb") as f:
                await update.message.reply_document(f, filename=os.path.basename(filename))
        else:
            with open(filename, "rb") as f:
                # если видео — send_video, если аудио — send_audio; для простоты отправляем документ
                await update.message.reply_video(f, supports_streaming=True)
        # 2) Дополнительно: извлекаем аудио в MP3 и отправляем отдельным файлом
        try:
            base_noext = filename.rsplit('.', 1)[0]
            audio_path = base_noext + '.mp3'
            # Извлекаем аудио дорожку без перекодирования видео (-vn)
            os.system(f'ffmpeg -y -i "{filename}" -vn -c:a libmp3lame -b:a 192k "{audio_path}"')
            if os.path.exists(audio_path):
                audio_size = os.path.getsize(audio_path)
                # Telegram обычно спокойно принимает MP3; если вдруг очень большой — отправим как документ
                if audio_size > 49 * 1024 * 1024:
                    with open(audio_path, "rb") as af:
                        await update.message.reply_document(af, filename=os.path.basename(audio_path), caption="Аудио дорожка (MP3)")
                else:
                    with open(audio_path, "rb") as af:
                        await update.message.reply_audio(af, caption="Аудио дорожка (MP3)")
                try:
                    os.remove(audio_path)
                except:
                    pass
        except Exception as e:
            # Не падаем из-за аудио — просто сообщим в чат
            try:
                await update.message.reply_text(f"Не удалось извлечь аудио: {e}")
            except:
                pass
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Ошибка при отправке: {e}")
    finally:
        # cleanup
        try:
            os.remove(filename)
        except:
            pass

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_and_send))
    print("Bot started")
    app.run_polling()