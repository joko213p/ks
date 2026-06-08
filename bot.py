import os
import re
import zipfile
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path

import instaloader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")  # optionnel : restreindre à un groupe


def extract_username(text: str) -> str | None:
    """Extrait le username Instagram depuis un lien ou un @username."""
    text = text.strip()

    # Lien complet : https://www.instagram.com/username/ ou https://instagram.com/username
    match = re.search(
        r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?",
        text,
    )
    if match:
        return match.group(1)

    # @username direct
    match = re.match(r"^@?([A-Za-z0-9_.]{1,30})$", text)
    if match:
        return match.group(1)

    return None


def download_posts(username: str, download_dir: str) -> dict:
    """
    Télécharge uniquement les posts (images + vidéos) d'un profil Instagram public.
    Exclut : stories, highlights, reels.
    Retourne un dict avec les stats et les chemins des fichiers.
    """
    L = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",  # pas de fichier texte
        filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}",
        quiet=True,
    )

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        raise ValueError(f"Le profil @{username} n'existe pas.")
    except instaloader.exceptions.LoginRequiredException:
        raise ValueError(
            f"Le profil @{username} est privé ou nécessite une connexion."
        )

    profile_dir = Path(download_dir) / username
    profile_dir.mkdir(parents=True, exist_ok=True)

    images = []
    videos = []
    errors = 0

    # On itère uniquement les posts (pas les reels, stories, highlights)
    for post in profile.get_posts():
        try:
            # Exclure les Reels (is_video + product_type == "clips")
            if post.is_video and getattr(post, "product_type", "") == "clips":
                continue

            L.download_post(post, target=profile_dir)

            # Collecter les fichiers téléchargés
            for f in profile_dir.iterdir():
                if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    if f not in images:
                        images.append(f)
                elif f.suffix.lower() in (".mp4", ".mov", ".avi"):
                    if f not in videos:
                        videos.append(f)

        except Exception as e:
            logger.warning(f"Erreur sur le post {post.shortcode}: {e}")
            errors += 1
            continue

    return {
        "profile_dir": profile_dir,
        "images": sorted(images),
        "videos": sorted(videos),
        "post_count": profile.mediacount,
        "full_name": profile.full_name,
        "errors": errors,
    }


def create_video_zip(videos: list, username: str, zip_path: str) -> str:
    """Crée un ZIP contenant toutes les vidéos."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for video in videos:
            zf.write(video, arcname=video.name)
    return zip_path


async def get_or_create_topic(bot, chat_id: int, username: str) -> int | None:
    """
    Crée ou retrouve un topic dans un groupe forum.
    Retourne le message_thread_id ou None si pas un forum.
    """
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=f"@{username}",
        )
        return topic.message_thread_id
    except TelegramError as e:
        if "not a forum" in str(e).lower() or "FORUM_DISABLED" in str(e):
            return None  # Le groupe n'est pas un forum, on envoie sans topic
        logger.warning(f"Impossible de créer le topic: {e}")
        return None


async def send_status(bot, chat_id: int, thread_id: int | None, text: str):
    """Envoie un message de statut."""
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": ParseMode.HTML}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    await bot.send_message(**kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Instagram Post Downloader Bot</b>\n\n"
        "Envoie-moi un lien ou un nom de profil Instagram :\n"
        "• <code>https://www.instagram.com/username</code>\n"
        "• <code>@username</code>\n"
        "• <code>username</code>\n\n"
        "Je téléchargerai tous les <b>posts</b> (photos + vidéos) du profil public.\n"
        "⚠️ Les stories, highlights et reels sont exclus."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat_id
    text = message.text.strip()

    username = extract_username(text)
    if not username:
        await message.reply_text(
            "❌ Je n'ai pas reconnu de profil Instagram.\n"
            "Envoie un lien (<code>https://instagram.com/username</code>) "
            "ou un nom d'utilisateur (<code>@username</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    # Créer ou trouver le topic
    thread_id = await get_or_create_topic(context.bot, chat_id, username)

    await send_status(
        context.bot,
        chat_id,
        thread_id,
        f"🔍 Recherche du profil <b>@{username}</b>...",
    )

    tmpdir = tempfile.mkdtemp(prefix="insta_")
    try:
        # Téléchargement dans un thread séparé pour ne pas bloquer
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, download_posts, username, tmpdir
        )

        images = result["images"]
        videos = result["videos"]
        full_name = result["full_name"]

        await send_status(
            context.bot,
            chat_id,
            thread_id,
            f"✅ Profil trouvé : <b>{full_name}</b> (@{username})\n"
            f"📸 {len(images)} image(s) | 🎬 {len(videos)} vidéo(s)\n"
            f"📤 Envoi en cours...",
        )

        # ── Envoi des images ──────────────────────────────────────────────
        if images:
            # Envoi par lots de 10 (limite Telegram)
            batch_size = 10
            for i in range(0, len(images), batch_size):
                batch = images[i : i + batch_size]
                media_group = []
                opened_files = []
                try:
                    from telegram import InputMediaPhoto

                    for img_path in batch:
                        f = open(img_path, "rb")
                        opened_files.append(f)
                        caption = (
                            f"📸 @{username} – {img_path.stem}"
                            if i == 0 and len(media_group) == 0
                            else None
                        )
                        media_group.append(InputMediaPhoto(media=f, caption=caption))

                    kwargs = {"chat_id": chat_id, "media": media_group}
                    if thread_id:
                        kwargs["message_thread_id"] = thread_id
                    await context.bot.send_media_group(**kwargs)
                finally:
                    for f in opened_files:
                        f.close()

        # ── Envoi des vidéos dans un ZIP ──────────────────────────────────
        if videos:
            zip_path = os.path.join(tmpdir, f"{username}_videos.zip")
            await loop.run_in_executor(
                None, create_video_zip, videos, username, zip_path
            )

            zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)

            if zip_size_mb <= 50:  # Limite Telegram pour les documents
                with open(zip_path, "rb") as zf:
                    kwargs = {
                        "chat_id": chat_id,
                        "document": zf,
                        "filename": f"{username}_videos.zip",
                        "caption": f"🎬 {len(videos)} vidéo(s) de @{username}",
                    }
                    if thread_id:
                        kwargs["message_thread_id"] = thread_id
                    await context.bot.send_document(**kwargs)
            else:
                # Trop lourd → envoyer les vidéos une par une
                await send_status(
                    context.bot,
                    chat_id,
                    thread_id,
                    f"⚠️ ZIP trop lourd ({zip_size_mb:.1f} MB), envoi vidéo par vidéo...",
                )
                for idx, vid_path in enumerate(videos, 1):
                    vid_size_mb = os.path.getsize(vid_path) / (1024 * 1024)
                    if vid_size_mb > 50:
                        await send_status(
                            context.bot,
                            chat_id,
                            thread_id,
                            f"⚠️ Vidéo {idx} trop lourde ({vid_size_mb:.1f} MB), ignorée.",
                        )
                        continue
                    with open(vid_path, "rb") as vf:
                        kwargs = {
                            "chat_id": chat_id,
                            "video": vf,
                            "caption": f"🎬 @{username} ({idx}/{len(videos)})",
                            "supports_streaming": True,
                        }
                        if thread_id:
                            kwargs["message_thread_id"] = thread_id
                        await context.bot.send_video(**kwargs)

        if not images and not videos:
            await send_status(
                context.bot,
                chat_id,
                thread_id,
                "😕 Aucun post trouvé pour ce profil (peut-être privé ou vide).",
            )
        else:
            await send_status(
                context.bot,
                chat_id,
                thread_id,
                f"✅ Terminé ! {len(images)} photo(s) et {len(videos)} vidéo(s) envoyées.",
            )

    except ValueError as e:
        await send_status(context.bot, chat_id, thread_id, f"❌ {e}")
    except Exception as e:
        logger.exception(f"Erreur inattendue pour @{username}: {e}")
        await send_status(
            context.bot,
            chat_id,
            thread_id,
            f"❌ Erreur inattendue : {e}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("La variable d'environnement BOT_TOKEN est manquante.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot démarré.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
