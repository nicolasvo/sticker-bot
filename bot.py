import base64
import json
import logging
import os
import re
import traceback

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from user import User, get_sticker_set_name
from sticker import add_sticker, delete_sticker, make_original_image, compress_image

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

# Replace 'YOUR_BOT_TOKEN' with your actual bot token
TOKEN = os.getenv("BOT_API_TOKEN")


def image_to_base64(image_path):
    with open(image_path, "rb") as image_file:
        encoded_image = base64.b64encode(image_file.read())
        return encoded_image.decode("utf-8")


def base64_to_image(base64_string, output_file_path):
    with open(output_file_path, "wb") as image_file:
        decoded_image = base64.b64decode(base64_string)
        image_file.write(decoded_image)


def list_files_sam(user_id):
    directory = "/tmp/"
    pattern = rf"^{user_id}_.*_input_sam\.jpeg$"
    files = os.listdir(directory)
    filtered_files = [file for file in files if re.match(pattern, file)]
    filtered_files.sort(
        key=lambda x: os.path.getmtime(os.path.join(directory, x)), reverse=True
    )
    return filtered_files


async def make_async_post(url, data):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=data, headers={"Content-Type": "application/json"}
        ) as response:
            text = await response.text()
            logger.info(
                "POST %s -> status=%s content_type=%s body_len=%d body_preview=%r",
                url,
                response.status,
                response.headers.get("Content-Type"),
                len(text),
                text[:300],
            )
            return text


def _parse_image_response(raw, context):
    try:
        j = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(
            "%s: response is not JSON (len=%d preview=%r): %s",
            context,
            len(raw),
            raw[:300],
            e,
        )
        raise
    if not isinstance(j, dict) or "image" not in j:
        logger.error(
            "%s: JSON missing 'image' key. keys=%s preview=%r",
            context,
            list(j.keys()) if isinstance(j, dict) else type(j).__name__,
            raw[:300],
        )
        raise KeyError(f"{context}: response missing 'image' key")
    return j["image"]


async def request_birefnet(input_path):
    payload = {
        "image": image_to_base64(input_path),
    }
    url = os.getenv("API_URL_BIREFNET")
    logger.info(
        "request_birefnet: posting %d-byte payload, url=%s",
        len(payload["image"]),
        url,
    )
    try:
        r = await make_async_post(url, json.dumps(payload))
        image_base64 = _parse_image_response(r, "request_birefnet attempt 1")
    except Exception as e:
        logger.warning("request_birefnet attempt 1 failed (%s), retrying", e)
        r = await make_async_post(url, json.dumps(payload))
        image_base64 = _parse_image_response(r, "request_birefnet attempt 2")
    return image_base64


async def request_grounding_dino_sam(input_path, text_prompt):
    payload = {
        "image": image_to_base64(input_path),
        "text_prompt": text_prompt,
    }
    url = os.getenv("API_URL_GROUNDING_DINO_SAM")
    logger.info(
        "request_grounding_dino_sam: posting %d-byte payload, prompt=%r, url=%s",
        len(payload["image"]),
        text_prompt,
        url,
    )
    try:
        r = await make_async_post(url, json.dumps(payload))
        image_base64 = _parse_image_response(r, "request_grounding_dino_sam attempt 1")
    except Exception as e:
        logger.warning("request_grounding_dino_sam attempt 1 failed (%s), retrying", e)
        r = await make_async_post(url, json.dumps(payload))
        image_base64 = _parse_image_response(r, "request_grounding_dino_sam attempt 2")
    return image_base64


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hi! I make stickers. Send me a picture 👨‍🎨")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = update.get_bot()
    user = User(update)
    user_message_id = f"{user.id}_{update.message.id}"
    input_path = f"/tmp/{user_message_id}_input.jpeg"
    output_path = f"/tmp/{user_message_id}_output.webp"
    output_png_path = f"/tmp/{user_message_id}_output.png"
    output_original_path = f"/tmp/{user_message_id}_output_original.webp"
    message_id = update.message.id
    if update.message.photo:
        await update.message.reply_text("🧮")

        keyboard = [
            [
                InlineKeyboardButton("Yes", callback_data=f"yes_{message_id}"),
                InlineKeyboardButton("No", callback_data=f"no_{message_id}"),
            ],
            [
                InlineKeyboardButton(
                    "Use original picture",
                    callback_data=f"original_{message_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Select from prompt", callback_data=f"sam_{message_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Send sticker as file", callback_data=f"file_{message_id}"
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        file_id = update.message.photo[-1].file_id
        media_message = await context.bot.get_file(file_id)
        await media_message.download_to_drive(input_path)

        try:
            logger.info("handle_message: user=%s message_id=%s input=%s", user.id, message_id, input_path)
            image_base64 = await request_birefnet(input_path)
            logger.info("handle_message: got image_base64 len=%d", len(image_base64) if image_base64 else 0)
            base64_to_image(image_base64, output_path)
            base64_to_image(image_base64, output_png_path)
            compress_image(output_path, output_path)
            await update.message.reply_text(
                "Do you want to add this sticker? ✍️",
            )
            await bot.send_sticker(
                user.id,
                output_path,
                reply_markup=reply_markup,
            )
            make_original_image(
                input_path,
                output_original_path,
            )
        except Exception as e:
            logger.error(
                "handle_message failed for user=%s message_id=%s: %s\n%s",
                user.id,
                message_id,
                e,
                traceback.format_exc(),
            )
            await update.message.reply_text(
                f"Something went wrong: {type(e).__name__}: {e}"
            )
    else:
        files = list_files_sam(user.id)
        if len(files) > 0:
            await update.message.reply_text("🪄")

            keyboard = [
                [
                    InlineKeyboardButton("Yes", callback_data=f"yes_{message_id}"),
                    InlineKeyboardButton("No", callback_data=f"no_{message_id}"),
                ],
                [
                    InlineKeyboardButton(
                        "Try another prompt",
                        callback_data=f"again_{message_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Send sticker as file", callback_data=f"file_{message_id}"
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            image_base64 = await request_grounding_dino_sam(
                f"/tmp/{files[0]}", text_prompt=update.message.text
            )
            if not image_base64:
                await update.message.reply_text(
                    f"'{update.message.text}' was not detected in the image.\nTry to write another text prompt 🤞"
                )
                return
            base64_to_image(image_base64, output_path)
            base64_to_image(image_base64, output_png_path)
            compress_image(output_path, output_path)
            await update.message.reply_text(
                "Do you want to add this sticker? ✍️",
            )
            await bot.send_sticker(
                user.id,
                output_path,
                reply_markup=reply_markup,
            )
        else:
            await update.message.reply_text("Send me a picture 👨‍🎨")


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    user = User(update)
    query = update.callback_query
    await query.edit_message_reply_markup(reply_markup=None)
    await query.answer()
    user_message_id = f"{user.id}_{query.data.split('_')[-1]}"
    input_path = f"/tmp/{user_message_id}_input.jpeg"
    input_sam_path = f"/tmp/{user_message_id}_input_sam.jpeg"
    output_path = f"/tmp/{user_message_id}_output.webp"
    output_png_path = f"/tmp/{user_message_id}_output.png"
    output_original_path = f"/tmp/{user_message_id}_output_original.webp"
    input_copy_path = f"/tmp/{user_message_id}_copy.webp"
    files = list_files_sam(user.id)
    data = query.data.split("_")[0]

    if data == "sam":
        os.rename(input_path, input_sam_path)
        await query.message.reply_text(
            "Write what you want to select from the image.\nFor example: person on the right, orange cat, dog 🖖"
        )
    elif data == "again":
        await query.message.reply_text(
            "Write what you want to select from the image.\nFor example: person on the right, orange cat, dog 🖖"
        )
    elif data == "yes":
        if not os.path.exists(output_path):
            print(f"file {output_path} not found")
            await query.message.reply_text(
                f"Sorry, request expired, send picture again 👇"
            )
            return
        await query.delete_message()
        await query.message.reply_text("Sticker added 👌")
        await add_sticker(
            user,
            update.get_bot(),
            output_path,
        )
        for f in files:
            os.remove(f"/tmp/{f}") if os.path.exists(f"/tmp/{f}") else None
    elif data == "no":
        await query.delete_message()
        await query.message.reply_text("Sticker discarded 🤌")
        for f in files:
            os.remove(f"/tmp/{f}") if os.path.exists(f"/tmp/{f}") else None
    elif data == "original":
        if not os.path.exists(output_original_path):
            print(f"file {output_original_path} not found")
            await query.message.reply_text(
                f"Sorry, request expired, send picture again 👇"
            )
            return
        await query.message.reply_text("Original sticker added ✌")
        await add_sticker(
            user,
            update.get_bot(),
            output_original_path,
        )
    elif data == "file":
        if not os.path.exists(output_png_path):
            print(f"file {output_png_path} not found")
            await query.message.reply_text(
                f"Sorry, request expired, send picture again 👇"
            )
            return
        await query.message.reply_text("File sent 👍")
        await query.message.reply_document(open(output_png_path, "rb"))
        for f in files:
            os.remove(f"/tmp/{f}") if os.path.exists(f"/tmp/{f}") else None
    elif data == "yescopy":
        if not os.path.exists(input_copy_path):
            print(f"file {input_copy_path} not found")
            await query.message.reply_text(
                f"Sorry, request expired, send picture again 👇"
            )
            return
        await query.message.reply_text("Sticker copied to your pack 💅")
        await add_sticker(
            user,
            update.get_bot(),
            input_copy_path,
        )
    os.remove(input_path) if os.path.exists(input_path) else None
    os.remove(output_path) if os.path.exists(output_path) else None
    os.remove(output_png_path) if os.path.exists(output_png_path) else None
    os.remove(output_original_path) if os.path.exists(output_original_path) else None
    os.remove(input_copy_path) if os.path.exists(input_copy_path) else None


async def handle_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.sticker:
        if update.message.sticker.is_animated or update.message.sticker.is_video:
            await update.message.reply_text("Sticker can't be animated 🙅‍♀️")
            return
        user = User(update)
        message_id = update.message.id
        user_message_id = f"{user.id}_{update.message.id}"
        input_copy_path = f"/tmp/{user_message_id}_copy.webp"
        file_id = update.message.sticker.file_id
        media_message = await context.bot.get_file(file_id)
        await media_message.download_to_drive(input_copy_path)

        keyboard = [
            [
                InlineKeyboardButton("Yes", callback_data=f"yescopy_{message_id}"),
                InlineKeyboardButton("No", callback_data=f"no_{message_id}"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Do you want to copy this sticker to your pack? 👆",
            reply_markup=reply_markup,
        )
    elif update.message.text:
        await update.message.reply_text(
            "Forward me a sticker you want to copy to your sticker pack 🙌"
        )


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.sticker:
        await delete_sticker(update, update.message.sticker.file_id)
    else:
        await update.message.reply_text(
            "Send me a sticker you want to delete 🤲", reply_markup=ForceReply()
        )


async def handle_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = User(update)
    sticker_set_name = await get_sticker_set_name(user, update.get_bot())
    link = f"https://t.me/addstickers/{sticker_set_name}"
    await update.message.reply_text(f"Link to sticker pack: {link}")


def main():
    application = Application.builder().token(TOKEN).concurrent_updates(True).build()

    # Add command handler
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Add picture handler
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))

    application.add_handler(CallbackQueryHandler(handle_choice))

    # Add a /start command handler
    application.add_handler(
        MessageHandler(filters.COMMAND & filters.Regex(r"/start"), start)
    )
    application.add_handler(
        MessageHandler(filters.COMMAND & filters.Regex(r"/getpack"), handle_pack)
    )
    application.add_handler(
        MessageHandler(
            filters.REPLY | (filters.COMMAND & filters.Regex(r"/delete")),
            handle_delete,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ATTACHMENT & ~filters.REPLY | filters.Regex(r"/copy"),
            handle_copy,
        )
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
