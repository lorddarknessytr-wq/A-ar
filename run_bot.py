"""
run_bot.py — مخزن «آرشیور»
----------------------------
این مخزن تنها جایی‌ست که getUpdates می‌زنه و offset رو جلو می‌بره —
باید فقط همین یک مخزن این کار رو بکنه (نه مخزن ادمین)، وگرنه دو
خواننده‌ی مستقل روی یک صف مشترک، پیام‌ها رو تکراری پردازش می‌کنن.

کارهایی که مستقیم و سریع خودش جواب می‌ده:
  - /start، /help، /ticket (و ارسال مستقیم تیکت به پیوی مالک)
  - عدد یا #عدد (ثبت درخواست فایل) و /file (تحویل فایل)
  - خوندن کانال منبع: عکس مود (#مود) و ویدیو (#ویدئو)
  - تشخیص اسپم (گزارش مستقیم به پیوی مالک)

کارهایی که خودش انجام نمی‌ده، فقط صف می‌کنه تا مخزن ادمین بخونه:
  - هر پیام دیگه‌ای که از طرف مالک باشه (پنل، بلاک، پست فوری، بگ‌ریپورت، ...)
    در state["pending_owner_commands"] ذخیره می‌شه.

این مخزن باید هر ۱ تا ۲ دقیقه اجرا بشه (زنگ‌زن بیرونی مثل cron-job.org).
"""

import os
import re
import sys
import uuid

import bot_core as core


def uuid_short():
    return uuid.uuid4().hex[:8]


def handle_source_channel_message(state, msg):
    file_info = msg.get("file")
    caption = msg.get("text") or ""
    message_id = msg.get("message_id")

    if file_info and file_info.get("file_type") and message_id:
        core.remember_recent_file(state, message_id, file_info.get("file_id"), file_info.get("file_type"))

    if not file_info and message_id:
        cached = core.recall_recent_file(state, message_id)
        if cached:
            file_info = {"file_type": cached["file_type"], "file_id": cached["file_id"]}
            print(f"DEBUG: فایل از حافظهٔ موقت بازیابی شد (پیام ادیت‌شده) -> {cached['file_type']}")

    if not file_info:
        return

    file_type = file_info.get("file_type")
    print(f"DEBUG: پیام کانال منبع -> file_type={file_type!r} caption={caption[:60]!r} | file_info خام کامل: {file_info}")

    mod_parsed = core.parse_mod_caption(caption)
    video_parsed = core.parse_video_caption(caption)

    if mod_parsed is not None:
        state["pending_photo"] = {
            "file_id": file_info.get("file_id"),
            "file_type": file_info.get("file_type"),
            "message_id": message_id,
            **mod_parsed,
        }

    elif video_parsed is not None or file_type == "Video":
        title = (video_parsed or {}).get("title") or caption.strip() or ""
        state["videos"].append({
            "id": uuid_short(),
            "video_file_id": file_info.get("file_id"),
            "file_type": file_info.get("file_type") or "Video",
            "message_id": message_id,
            "source_channel_guid": msg.get("chat_id"),
            "title": title,
        })

    else:
        file_number = core.extract_number(caption)
        pending = state.get("pending_photo")

        if not file_number and pending:
            file_number = pending.get("number")

        if not file_number:
            print("DEBUG: فایل بدون هشتگ شماره (#عدد) و بدون عکس در انتظار، نادیده گرفته شد")
            return

        original_name = (
            file_info.get("file_name") or file_info.get("name")
            or file_info.get("original_name") or file_info.get("title")
        )
        manual_ext = core.extract_extension_line(caption) or (pending.get("extension") if pending else "")

        state["files_by_number"][file_number] = {
            "file_id": file_info.get("file_id"),
            "file_type": core.normalize_send_file_type(file_info.get("file_type")),
            "file_name": original_name,
            "manual_extension": manual_ext,
            "message_id": message_id,
            "source_channel_guid": msg.get("chat_id"),
            "title": (pending.get("title") if pending else None) or f"فایل شماره {file_number}",
        }

        if pending and pending.get("number") == file_number:
            state["mods"].append({
                "id": uuid_short(),
                "photo_file_id": pending["file_id"],
                "photo_file_type": pending.get("file_type"),
                "photo_message_id": pending.get("message_id"),
                "source_channel_guid": msg.get("chat_id"),
                "title": pending["title"],
                "description": pending["description"],
                "version": pending["version"],
                "number": file_number,
            })
            state["pending_photo"] = None
        elif pending and pending.get("number") != file_number:
            print(f"DEBUG: شمارهٔ فایل (#{file_number}) با شمارهٔ عکسِ در انتظار "
                  f"(#{pending.get('number')}) یکی نیست؛ عکس همچنان منتظر می‌مونه")
        else:
            print(f"DEBUG: فایل #{file_number} بدون عکس همراه، فقط برای دریافت مستقیم ذخیره شد")


def handle_start_command(token, config, state, chat_id):
    welcome = config.get(
        "start_text",
        "👋 سلام و خوش اومدید!\n\n"
        "برای دریافت هر مود، شماره‌ای که زیر همون پست توی کانال نوشته شده "
        "رو برام بفرستید (مثلاً 9 یا #9)، بعد /file رو بزنید تا فایلش براتون بیاد.\n\n"
        "🎫 اگه با پشتیبانی کاری داشتید، با /ticket می‌تونید برام پیام بذارید."
    )
    channels = config.get("required_join_channels", [])
    if channels:
        welcome += "\n\n" + core.build_join_prompt(channels)
    core.send_message(token, chat_id, welcome)


NUMBER_REQUEST_RE = re.compile(r"^(?:[#/])?(\d+)$")
NUMBER_REQUEST_COOLDOWN_MINUTES = 10


def handle_number_request(token, config, state, chat_id, text):
    m = NUMBER_REQUEST_RE.match((text or "").strip())
    if not m:
        return False

    number = m.group(1)
    entry = state.get("files_by_number", {}).get(number)
    if not entry:
        core.send_message(token, chat_id, f"فایلی با شمارهٔ {number} پیدا نشد.")
        return True

    if not core.should_send_now(state, f"numreq:{chat_id}:{number}", NUMBER_REQUEST_COOLDOWN_MINUTES):
        print(f"DEBUG: درخواست تکراری #{number} از {chat_id} — نادیده گرفته شد.")
        return True

    state.setdefault("pending_file_requests", {})[chat_id] = {
        "number": number,
        "requested_at": core.tehran_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    core.send_message(token, chat_id, core.build_join_prompt(config.get("required_join_channels", [])))
    return True


def handle_file_command(token, config, state, chat_id):
    if not chat_id:
        return True

    pending = state.setdefault("pending_file_requests", {}).get(chat_id)
    if not pending:
        core.send_message(token, chat_id, "⚠️ اول شمارهٔ مود را بفرستید؛ سپس برای دریافت آن /file را بزنید.")
        return True

    number = str(pending.get("number", "")).strip()
    entry = state.get("files_by_number", {}).get(number)
    state["pending_file_requests"].pop(chat_id, None)

    if not entry:
        core.send_message(token, chat_id, f"فایل مود شمارهٔ {number} دیگر در انبار ربات پیدا نشد.")
        return True

    core.send_file(
        token, chat_id, entry["file_id"], f"#{number}",
        file_type=entry.get("file_type") or "File",
        file_name=f"{number}{entry.get('manual_extension') or core.file_extension(entry.get('file_name'))}",
        source_chat_id=entry.get("source_channel_guid"), source_message_id=entry.get("message_id"),
    )
    return True


def handle_ticket_flow(token, config, state, chat_id, text):
    awaiting = state.setdefault("awaiting_ticket", [])

    if chat_id in awaiting:
        awaiting.remove(chat_id)
        now = core.tehran_now().strftime("%Y-%m-%d %H:%M")
        core.send_message(
            token, config["owner_guid"],
            f"🎫 تیکت جدید\nGUID فرستنده: {chat_id}\nزمان: {now}\nمتن: {text}"
        )
        core.send_message(token, chat_id, config.get("texts", {}).get("ticket_sent", "تیکت شما ارسال شد. با تشکر 🙏"))
        return True

    if text == "/ticket":
        if chat_id not in awaiting:
            awaiting.append(chat_id)
        core.send_message(token, chat_id, config.get("texts", {}).get("ticket_prompt", "لطفاً متن تیکت خودتون رو بنویسید."))
        return True

    return False


def main():
    config = core.load_config()
    state = core.load_state()
    token = os.environ.get("RUBIKA_BOT_TOKEN") or config.get("bot_token")
    if not token:
        print("DEBUG: توکن پیدا نشد")
        return

    core.track_channel_activation(state, config)
    core.prune_known_users(state, config)

    try:
        resp = core.get_updates(token, offset_id=state.get("last_offset_id"), limit=50)
        updates = resp.get("updates", []) if isinstance(resp, dict) else []
        next_offset = resp.get("next_offset_id") if isinstance(resp, dict) else None
    except Exception as e:
        core.log_error(state, "دریافت آپدیت‌ها", e)
        updates = []
        next_offset = None

    print(f"DEBUG: تعداد آپدیت‌های دریافتی: {len(updates)} | next_offset={next_offset!r}")

    for update in updates:
        msg = update.get("new_message") or update.get("updated_message") or update
        if not isinstance(msg, dict):
            continue
        update_identity = core.get_update_identity(update, msg)
        if core.was_processed(state, update_identity):
            print(f"DEBUG: آپدیت تکراری رد شد -> {update_identity}")
            continue
        chat_id = msg.get("chat_id") or update.get("chat_id")
        text = (msg.get("text") or "").strip()
        print(f"DEBUG: پیام -> chat_id={chat_id} | text={text!r}")

        try:
            if chat_id and chat_id != config.get("owner_guid"):
                block_info = core.is_blocked(state, chat_id)
                if block_info:
                    core.send_message(token, chat_id, core.build_blocked_message(block_info))
                    continue

            is_new_user = core.track_known_user(state, config, chat_id)
            if is_new_user:
                try:
                    core.set_chat_keypad(token, chat_id, ["/help", "/ticket"])
                except Exception as e:
                    core.log_error(state, "تنظیم کیبورد ثابت", e)

            if chat_id and chat_id not in (config.get("owner_guid"), config.get("source_channel_guid")):
                if core.check_spam(state, chat_id) and core.should_send_now(state, f"spamreport:{chat_id}", core.SPAM_REPORT_COOLDOWN_MINUTES):
                    core.send_message(
                        token, config["owner_guid"],
                        f"🚨 فعالیت مشکوک/اسپم\nGUID فرد: {chat_id}\n"
                        f"بیش از {core.SPAM_THRESHOLD} پیام در {core.SPAM_WINDOW_MINUTES} دقیقهٔ اخیر."
                    )

            if text == "/myidver001" and chat_id:
                core.send_message(token, chat_id, f"GUID این چت:\n{chat_id}")
            elif text == "/start" and chat_id and chat_id not in (config.get("owner_guid"), config.get("source_channel_guid")):
                if core.should_send_now(state, f"start:{chat_id}", 3):
                    handle_start_command(token, config, state, chat_id)
            elif text == "/help" and chat_id and chat_id != config.get("owner_guid"):
                if core.should_send_now(state, f"help:{chat_id}", 3):
                    help_text = config.get("help_text", "برای دریافت فایل مود، شمارهٔ زیر پست رو با # یا / به من بفرستید (مثلاً #1).")
                    core.send_message(token, chat_id, help_text)
            elif chat_id and chat_id != config.get("owner_guid") and handle_ticket_flow(token, config, state, chat_id, text):
                pass
            elif text == "/file" and chat_id:
                handle_file_command(token, config, state, chat_id)
            elif not msg.get("file") and chat_id != config.get("owner_guid") and handle_number_request(token, config, state, chat_id, text):
                pass
            elif chat_id and msg.get("file") and chat_id in (config.get("source_channel_guid"), config.get("owner_guid")):
                handle_source_channel_message(state, msg)
            elif chat_id and chat_id == config.get("owner_guid"):
                # این مخزن دستورهای مالک رو خودش اجرا نمی‌کنه؛ فقط صف
                # می‌کنه تا مخزن ادمین بخونه و پردازش کنه.
                core.queue_owner_command(state, chat_id, text)
                print(f"DEBUG: دستور مالک صف شد برای مخزن ادمین: {text!r}")
        except Exception as e:
            core.log_error(state, "پردازش پیام", e)
        finally:
            core.mark_processed(state, update_identity)

    if next_offset:
        state["last_offset_id"] = next_offset
    else:
        print("DEBUG: next_offset_id خالی بود؛ offset قبلی حفظ شد.")

    core.trim_stored_content(state)
    core.prune_sent_log(state)

    print(f"DEBUG: وضعیت قبل از ذخیره -> last_offset_id={state.get('last_offset_id')!r} "
          f"mods={len(state.get('mods', []))} videos={len(state.get('videos', []))} "
          f"pending_owner_commands={len(state.get('pending_owner_commands', []))}")

    core.save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as fatal:
        print(f"خطای کلی و غیرمنتظره: {fatal}", file=sys.stderr)
        raise
