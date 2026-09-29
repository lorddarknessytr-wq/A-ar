"""
bot_core.py
------------
با متدهای رسمی روبیکا (فقط requests) کار می‌کند.

تغییرات این نسخه:
- پارس کپشن «موقعیتی» و دقیق: خط‌به‌خط، بدون نیاز به برچسب‌هایی مثل
  «توضیحات:» — فقط باید تگ #مود (برای عکس مود) یا #ویدئو (برای ویدیو)
  جایی در کپشن باشد.
- جلوگیری از سیل پیام با یک «دفترچهٔ ارسال» سبک (sent_log): قبل از هر
  پیام دوره‌ای (مثل پیام راه‌اندازی)، چک می‌شود که در N دقیقهٔ اخیر
  فرستاده نشده باشد؛ ورودی‌های قدیمی‌تر از ۱ ساعت خودکار پاک می‌شوند.
- سوییچ per-channel «send_file_directly»: روشن = فایل مستقیم در کانال
  پست شود؛ خاموش (پیش‌فرض) = فقط لینک دریافت از ربات نشان داده شود.
"""

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

TEHRAN_OFFSET = timedelta(hours=3, minutes=30)
MAX_ERRORS_STORED = 200
ERRORS_PER_PAGE = 5
ERROR_NOTIFY_COOLDOWN_MINUTES = 10
REQUEST_TIMEOUT = 15
UPLOAD_TIMEOUT = 90
MAX_PROCESSED_IDS = 1000
MAX_STORED_MODS = 800
MAX_STORED_VIDEOS = 800


# ---------------------------------------------------------------------------
# فایل‌های JSON
# ---------------------------------------------------------------------------
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    return load_json(CONFIG_PATH)


def load_state():
    return load_json(STATE_PATH)


def save_state(state):
    save_json(STATE_PATH, state)


def tehran_now():
    return datetime.now(timezone.utc) + TEHRAN_OFFSET


# ---------------------------------------------------------------------------
# ارتباط خام با Rubika Bot API
# ---------------------------------------------------------------------------
def api_call(token, method, payload=None, retries=3, backoff_seconds=2):
    """
    فراخوانی متد API روبیکا. خطاهای موقتِ خودِ سرور روبیکا (502/503/504،
    یعنی سرور لحظه‌ای شلوغ/داون بوده، نه اشکال ما) تا ۳ بار با فاصله
    دوباره امتحان می‌شن؛ فقط بعد از شکستِ همهٔ تلاش‌ها خطا بالا می‌ره.
    """
    import time as _time
    url = f"https://botapi.rubika.ir/v3/{token}/{method}"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload or {}, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (502, 503, 504):
                last_exc = RuntimeError(f"{resp.status_code} موقت از سرور روبیکا (تلاش {attempt}/{retries})")
                _time.sleep(backoff_seconds * attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", body) if isinstance(body, dict) else body
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            _time.sleep(backoff_seconds * attempt)
            continue
    raise last_exc or RuntimeError(f"فراخوانی {method} بدون دلیل مشخص شکست خورد")


def get_me(token):
    return api_call(token, "getMe")


FORMAT_TYPES = {
    "bold": "Bold", "italic": "Italic", "underline": "Underline",
    "strike": "Strike", "spoiler": "Spoiler", "mono": "Mono",
    "pre": "Pre", "quote": "Quote",
}


def _utf16_len(s):
    """طول رشته بر حسب واحدهای UTF-16 (مثل تلگرام) — هر ایموجی/کاراکتر
    خارج از BMP دو واحد حساب می‌شه، نه یکی. قبلاً از len() پایتون
    (تعداد کاراکتر خام) استفاده می‌شد که وقتی قبل از بخش فرمت‌شده
    ایموجی بود، مرز بولد/نقل‌قول رو جابه‌جا می‌کرد و باعث می‌شد بخشی از
    متن از داخل باکس «بیرون بزنه»."""
    return len(s.encode("utf-16-le")) // 2


def build_text_with_metadata(parts):
    """
    parts: لیستی از تاپل (متن, نوع‌فرمت یا None). نوع‌فرمت یکی از کلیدهای
    FORMAT_TYPES (مثلاً "bold", "quote"). خروجی: (متن نهایی، آرایهٔ
    metadata برای فرستادن به روبیکا). افست‌ها بر اساس واحد UTF-16
    حساب می‌شن (همون قراردادی که تلگرام و به‌احتمال زیاد خودِ روبیکا
    استفاده می‌کنن) تا مرز بولد/نقل‌قول با وجود ایموجی هم دقیق بمونه.
    """
    text = ""
    metadata = []
    pos16 = 0
    for chunk, fmt in parts:
        if not chunk:
            continue
        text += chunk
        length16 = _utf16_len(chunk)
        if fmt:
            metadata.append({"type": FORMAT_TYPES[fmt], "from_index": pos16, "length": length16})
        pos16 += length16
    return text, metadata


_CUSTOM_QUOTE_RE = re.compile(r"/Quote(.*?)/Quote", re.IGNORECASE | re.DOTALL)


def parse_custom_markup(text):
    """
    برای متن‌های کاستوم داخل config.json: هر بخشی بین دو تا /Quote رو
    به‌صورت نقل‌قول علامت می‌زنه. خروجی مستقیم قابل‌استفاده توی
    build_text_with_metadata یا اضافه‌شدن به لیست parts هست.
    """
    if not text:
        return []
    parts = []
    last = 0
    for m in _CUSTOM_QUOTE_RE.finditer(text):
        if m.start() > last:
            parts.append((text[last:m.start()], None))
        if m.group(1):
            parts.append((m.group(1), "quote"))
        last = m.end()
    if last < len(text):
        parts.append((text[last:], None))
    return parts


def send_message(token, chat_id, text, metadata=None):
    payload = {"chat_id": chat_id, "text": text}
    if metadata:
        payload["metadata"] = {"meta_data_parts": metadata}
    return api_call(token, "sendMessage", payload)


def get_file(token, file_id):
    return api_call(token, "getFile", {"file_id": file_id})


def request_send_file(token, file_type):
    return api_call(token, "requestSendFile", {"type": file_type or "File"})


def forward_message(token, from_chat_id, message_id, to_chat_id):
    return api_call(token, "forwardMessage", {
        "from_chat_id": from_chat_id, "message_id": message_id, "to_chat_id": to_chat_id,
    })


def _request_with_retry(method, url, retries=3, backoff_seconds=3, **kwargs):
    """درخواست خام (دانلود/آپلود فایل) با تلاش مجدد. قبلاً این نوع
    درخواست‌ها هیچ retry ای نداشتن — یک قطعی شبکهٔ لحظه‌ای (Timeout/
    Connection aborted) کافی بود کل ارسال شکست بخوره؛ حالا مثل api_call
    چند بار با فاصله دوباره امتحان می‌شن."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            print(f"DEBUG: درخواست {method} به {url[:60]}... ناموفق (تلاش {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise last_exc


def reupload_file(token, file_id, file_type, file_name="file"):
    """دانلود فایل با file_id قدیمی و آپلود دوباره‌اش تا file_id تازه و
    معتبر برای چتِ مقصدِ جدید بگیریم (چون file_id یک چت همیشه برای چت
    دیگه معتبر نیست)."""
    file_info = get_file(token, file_id)
    download_url = (file_info or {}).get("download_url")
    if not download_url:
        raise RuntimeError("getFile آدرس دانلود برنگردوند.")
    r = _request_with_retry("GET", download_url, timeout=UPLOAD_TIMEOUT)
    upload_req = request_send_file(token, normalize_send_file_type(file_type))
    upload_url = (upload_req or {}).get("upload_url")
    if not upload_url:
        raise RuntimeError("requestSendFile آدرس آپلود برنگردوند.")
    up = _request_with_retry("POST", upload_url, timeout=UPLOAD_TIMEOUT, files={"file": (file_name, r.content)})
    result = up.json()
    new_file_id = result.get("file_id") or (result.get("data") or {}).get("file_id")
    if not new_file_id:
        raise RuntimeError(f"آپلود مجدد جواب معتبر نداد: {result}")
    return new_file_id


def normalize_send_file_type(file_type):
    """نوع‌های ناشناخته مثل Application را به File تبدیل می‌کند."""
    value = str(file_type or "File").strip()
    if value.lower() in {"image", "video", "file"}:
        return value.title()
    return "File"


def safe_file_name(file_name, fallback="file"):
    name = str(file_name or "").strip()
    if not name:
        return fallback
    return name.replace("/", "_").replace("\\", "_")[:180]


def file_extension(file_name):
    """پسوند فایل اصلی را با نقطه برمی‌گرداند (مثلاً '.apk')؛ اگر
    نداشت یا نامعتبر بود، رشتهٔ خالی برمی‌گرداند."""
    name = str(file_name or "").strip()
    if "." not in name:
        return ""
    ext = "." + name.rsplit(".", 1)[-1]
    if len(ext) > 10 or " " in ext:
        return ""
    return ext


def send_file(token, chat_id, file_id, text="", file_type=None, file_name="file",
              source_chat_id=None, source_message_id=None, metadata=None):
    """ارسال فایل با اولویت مسیرهای سریع و با نوع فایل استاندارد.
    metadata فقط روی تلاش مستقیم و re-upload اثر داره؛ اگه به فوروارد
    از کانال منبع افتاد، کپشن اصلی همون‌جا عیناً حفظ می‌شه (بدون
    فرمت‌دهی سفارشی ما).

    توجه: این تابع کل زنجیره (مستقیم → forward → re-upload) را فقط
    یک‌بار امتحان می‌کند، نه چند بار. تکرارِ کل زنجیره خطرناکه: اگه یه
    تلاش واقعاً موفق بشه ولی فقط تشخیص «موفقیت» ما خطا بده، تکرار کل
    زنجیره باعث پست دوبارهٔ همون فایل توی کانال می‌شه. قطعی‌های موقتِ
    شبکه از قبل در سطح پایین‌تر (api_call و دانلود/آپلود فایل) چند بار
    retry می‌شن، پس نیازی به تکرار کل زنجیره در این سطح نیست."""
    send_type = normalize_send_file_type(file_type)
    safe_name = safe_file_name(file_name, "file")

    def _try(fid, use_metadata=True):
        payload = {
            "chat_id": chat_id,
            "file_id": fid,
            "text": text,
        }
        if use_metadata and metadata:
            payload["metadata"] = {"meta_data_parts": metadata}
        result = api_call(token, "sendFile", payload)
        if not isinstance(result, dict):
            raise RuntimeError(f"پاسخ sendFile نامعتبر است: {result}")
        msg_id = (
            result.get("message_id") or result.get("new_message_id")
            or (result.get("new_message") or {}).get("message_id")
        )
        if not msg_id:
            raise RuntimeError(f"پاسخ sendFile بدون message_id: {result}")
        return result

    try:
        result = _try(file_id)
        print(f"DEBUG: sendFile موفق (مستقیم) -> chat_id={chat_id}")
        return result
    except Exception as direct_error:
        print(f"DEBUG: sendFile مستقیم ناموفق: {direct_error}")
        if metadata:
            try:
                result = _try(file_id, use_metadata=False)
                print(f"DEBUG: sendFile بدون metadata موفق -> chat_id={chat_id}")
                return result
            except Exception as no_meta_error:
                print(f"DEBUG: بدون metadata هم ناموفق: {no_meta_error}")

    if source_chat_id and source_message_id:
        try:
            fwd = forward_message(token, source_chat_id, source_message_id, chat_id)
            if isinstance(fwd, dict) and (
                fwd.get("new_message_id") or fwd.get("message_id")
                or (fwd.get("new_message") or {}).get("message_id")
            ):
                print(f"DEBUG: forwardMessage موفق -> chat_id={chat_id}")
                return fwd
            print(f"DEBUG: forwardMessage پاسخ قابل‌تأیید نداد: {fwd}")
        except Exception as forward_error:
            print(f"DEBUG: forwardMessage ناموفق: {forward_error}")

    new_file_id = reupload_file(token, file_id, send_type, safe_name)
    result = _try(new_file_id)
    print(f"DEBUG: sendFile بعد از آپلود مجدد موفق -> chat_id={chat_id}")
    return result


def get_updates(token, offset_id=None, limit=50):
    payload = {"limit": limit}
    if offset_id:
        payload["offset_id"] = offset_id
    return api_call(token, "getUpdates", payload)


# ---------------------------------------------------------------------------
# شناسایی کاربر در برابر کانال
# ---------------------------------------------------------------------------
def get_update_identity(update, msg=None):
    if isinstance(update, dict):
        for key in ("update_id", "id"):
            if update.get(key) is not None:
                return f"u:{update[key]}"
    if isinstance(msg, dict):
        for key in ("message_id", "id"):
            if msg.get(key) is not None:
                return f"m:{msg[key]}"
    return None


def was_processed(state, identity):
    return bool(identity and identity in state.setdefault("processed_updates", []))


def mark_processed(state, identity):
    if not identity:
        return
    items = state.setdefault("processed_updates", [])
    if identity not in items:
        items.append(identity)
    if len(items) > MAX_PROCESSED_IDS:
        del items[:-MAX_PROCESSED_IDS]


def known_channel_guids(config):
    guids = {config.get("source_channel_guid")}
    for ch in config.get("destination_channels", []):
        guids.add(ch.get("guid"))
    guids.discard(None)
    return guids


def track_known_user(state, config, chat_id):
    if not chat_id or chat_id in known_channel_guids(config):
        return False
    if chat_id == config.get("owner_guid"):
        return False
    users = state.setdefault("known_users", [])
    if chat_id not in users:
        users.append(chat_id)
        return True
    return False


def prune_known_users(state, config):
    """ورودی‌هایی که در واقع کانال هستن (مثلاً چون قبلاً GUID اشتباه بوده)
    یا مالک ربات هستن رو از known_users پاک می‌کند — خودترمیم‌شونده."""
    channels = known_channel_guids(config)
    owner = config.get("owner_guid")
    users = state.get("known_users", [])
    state["known_users"] = [u for u in users if u not in channels and u != owner]


# ---------------------------------------------------------------------------
# حافظهٔ موقت فایل‌های اخیر — برای پیام‌هایی که بعداً ادیت می‌شوند و در
# آپدیتِ ادیت، اطلاعات فایل همراهش نیست (فقط متن جدید می‌آید)
# ---------------------------------------------------------------------------
RECENT_FILES_TTL_MINUTES = 120


def remember_recent_file(state, message_id, file_id, file_type):
    if not message_id:
        return
    cache = state.setdefault("recent_files", {})
    cache[str(message_id)] = {
        "file_id": file_id,
        "file_type": file_type,
        "seen": tehran_now().strftime("%Y-%m-%d %H:%M"),
    }
    # پاک‌سازی ورودی‌های قدیمی‌تر از RECENT_FILES_TTL_MINUTES
    cutoff = tehran_now().replace(tzinfo=None) - timedelta(minutes=RECENT_FILES_TTL_MINUTES)
    for k in list(cache.keys()):
        try:
            if datetime.strptime(cache[k]["seen"], "%Y-%m-%d %H:%M") < cutoff:
                del cache[k]
        except Exception:
            del cache[k]


def recall_recent_file(state, message_id):
    if not message_id:
        return None
    return state.get("recent_files", {}).get(str(message_id))


# ---------------------------------------------------------------------------
# پارس کپشن — موقعیتی و دقیق
# ---------------------------------------------------------------------------
def _content_lines(caption: str):
    lines = [l.strip() for l in (caption or "").splitlines() if l.strip()]
    return [l for l in lines if not l.startswith("#")]


_LABEL_RE = re.compile(r"^\s*(عنوان|توضیحات|توضیح|ورژن|نسخه)\s*[:：]\s*")


def _strip_label(line: str) -> str:
    """اگر خط با برچسبی مثل «عنوان:» شروع شده باشه، برچسب رو حذف می‌کنه؛
    اگر نه، خودِ خط رو بدون تغییر برمی‌گردونه. یعنی هم فرمت با برچسب و
    هم بدون برچسب پشتیبانی می‌شه."""
    return _LABEL_RE.sub("", line).strip()


_MOD_TAG_RE = re.compile(r"(?:^|\s)#مود(?:\s|$)")
_VIDEO_TAG_RE = re.compile(r"(?:^|\s)#(?:ویدئو|ویدیو)(?:\s|$)")
_EXTENSION_RE = re.compile(r"^\s*(?:پسوند|فرمت|extension|ext)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)


def normalize_extension(value):
    """ورودی مثل 'mcaddon' یا '.mcaddon' یا 'MCPACK' را به '.mcaddon' استاندارد می‌کند."""
    value = str(value or "").strip().lstrip(".")
    if not value or " " in value or len(value) > 12:
        return ""
    return "." + value.lower()


def parse_mod_caption(caption: str):
    caption = caption or ""
    if not _MOD_TAG_RE.search(caption):
        return None

    lines = _content_lines(caption)
    if not lines:
        return {"title": "", "description": "", "version": "", "extension": "", "number": extract_number(caption)}

    title = _strip_label(lines[0])
    body = lines[1:]
    version_index = None
    version_value = ""
    extension_index = None
    extension_value = ""

    # نسخه را هم با «ورژن: ...» و هم با عددهایی مثل 1.21 پیدا می‌کنیم.
    version_re = re.compile(r"^\s*(?:ورژن|نسخه)\s*[:：]?\s*(.+?)\s*$")
    plain_version_re = re.compile(r"^\s*v?\d+(?:\.\d+){1,4}(?:[-+][\w.-]+)?\s*$", re.I)

    for idx, line in enumerate(body):
        ext_m = _EXTENSION_RE.match(line)
        if ext_m:
            extension_index = idx
            extension_value = normalize_extension(ext_m.group(1))
            continue
        if version_index is not None:
            continue
        m = version_re.match(line)
        if m:
            version_index = idx
            version_value = m.group(1).strip()
            continue
        if plain_version_re.match(line):
            version_index = idx
            version_value = line.strip()

    skip = {i for i in (version_index, extension_index) if i is not None}
    description_lines = [l for i, l in enumerate(body) if i not in skip]
    description = "\n".join(_strip_label(x) for x in description_lines if _strip_label(x)).strip()

    return {
        "title": title,
        "description": description,
        "version": version_value,
        "extension": extension_value,
        "number": extract_number(caption),
    }



def parse_video_caption(caption: str):
    """فرمت مورد انتظار:
    عنوان
    #ویدئو
    اگر تگ دقیق #ویدئو/#ویدیو نباشد، None برمی‌گردد."""
    caption = caption or ""
    if not _VIDEO_TAG_RE.search(caption):
        return None
    lines = _content_lines(caption)
    title = _strip_label(lines[0]) if lines else "ویدیو جدید"
    return {"title": title}


def extract_number(caption: str):
    """شمارهٔ بعد از # را از هر کپشنی (عکس یا فایل) استخراج می‌کند."""
    m = re.search(r"#(\d+)\b", caption or "")
    return m.group(1) if m else None


def extract_extension_line(caption: str):
    """اگر کپشن (عکس یا خودِ فایل) یک خط 'پسوند: xxx' داشته باشه، پسوند
    نرمال‌شده رو برمی‌گردونه؛ وگرنه رشتهٔ خالی."""
    for line in _content_lines(caption or ""):
        m = _EXTENSION_RE.match(line)
        if m:
            return normalize_extension(m.group(1))
    return ""


# ---------------------------------------------------------------------------
# ارسال مود / ویدیو به یک کانال مقصد
# ---------------------------------------------------------------------------
def send_mod(token, channel, mod, state):
    direct = channel.get("send_file_directly", False)
    parts = []

    if mod.get("title"):
        parts.append((mod["title"], "bold"))
        parts.append(("\n\n", None))

    if mod.get("description"):
        parts.append((f"⚙️- {mod['description']}", "quote"))
        # توجه: بین توضیح و طریقهٔ دانلود عمداً خط خالی نمی‌گذاریم؛ چون
        # هر کدوم metadata جدای خودشونو دارن، دو تا باکس نقل‌قولِ جدا
        # دیده می‌شن نه یکی ادامه‌ی هم. اگه به‌جای این، یکی دیده شد،
        # همین‌جا به‌جای "\n" بذار "\n\n".
        parts.append(("\n", None))

    if not direct and mod.get("number"):
        parts.append((
            f"برای دریافت فایل، عدد {mod['number']} یا #{mod['number']} رو برای ربات (@TLP_AdminBot) بفرستید.",
            "quote",
        ))

    if mod.get("version"):
        parts.append(("\n\n", None))
        parts.append((f"💾- ورژن: {mod['version']}", None))

    parts.append(("\n\n", None))
    parts.append((f" {channel['channel_link']}", None))

    if channel.get("mod_photo_extra_text"):
        parts.append(("\n\n", None))
        parts.extend(parse_custom_markup(channel["mod_photo_extra_text"]))

    text, metadata = build_text_with_metadata(parts)

    source_guid = mod.get("source_channel_guid")
    send_file(
        token, channel["guid"], mod["photo_file_id"], text, metadata=metadata,
        file_type=mod.get("photo_file_type") or "Image", file_name="cover.jpg",
        source_chat_id=source_guid, source_message_id=mod.get("photo_message_id"),
    )

    if direct and mod.get("number"):
        entry = state.get("files_by_number", {}).get(mod["number"])
        if entry and entry.get("file_id"):
            caption_text, caption_meta = build_text_with_metadata(
                parse_custom_markup(channel.get("mod_file_caption", ""))
            )
            send_file(
                token, channel["guid"], entry["file_id"], caption_text, metadata=caption_meta,
                file_type=entry.get("file_type") or "File",
                file_name=f"{mod['number']}{entry.get('manual_extension') or file_extension(entry.get('file_name'))}",
                source_chat_id=source_guid, source_message_id=entry.get("message_id"),
            )


def send_video(token, channel, video):
    # دکمهٔ روشن/خاموش ویدیو برای این کانال (در config.json هر کانال:
    # "videos_enabled": false برای خاموش کردن).
    if not channel.get("videos_enabled", True):
        print(f"DEBUG: ارسال ویدیو برای {channel.get('name', channel.get('guid'))} خاموشه (videos_enabled=false)")
        return

    parts = []
    if video.get("title"):
        parts.append((video["title"], None))

    if channel.get("channel_link"):
        if parts:
            parts.append(("\n\n", None))
        parts.append((channel["channel_link"], "quote"))

    if channel.get("video_extra_text"):
        if parts:
            parts.append(("\n\n", None))
        parts.extend(parse_custom_markup(channel["video_extra_text"]))

    text, metadata = build_text_with_metadata(parts)
    send_file(
        token, channel["guid"], video["video_file_id"], text, metadata=metadata,
        file_type=video.get("file_type") or "Video", file_name="video.mp4",
        source_chat_id=video.get("source_channel_guid"), source_message_id=video.get("message_id"),
    )


def pick_item(items, used_ids):
    if not items:
        return None, used_ids
    available = [i for i in items if i["id"] not in used_ids]
    if not available:
        used_ids = []
        available = items
    chosen = __import__("random").choice(available)
    return chosen, used_ids + [chosen["id"]]


# ---------------------------------------------------------------------------
# جلوگیری از سیل پیام‌های دوره‌ای (دفترچهٔ ارسال سبک)
# ---------------------------------------------------------------------------
def prune_sent_log(state, max_age_hours=1):
    """حذف رکوردهای قدیمی‌تر از max_age_hours از دفترچهٔ ارسال (مثل
    یادداشت‌های تکراری «ربات فعال است»/heartbeat) تا فایل state.json
    سنگین نشه. جدا از should_send_now هم قابل‌فراخوانیه تا این پاک‌سازی
    هر اجرا تضمینی انجام بشه، نه فقط وقتی یک کلید خاص چک می‌شه."""
    log = state.get("sent_log", {})
    if not log:
        return
    now = tehran_now().replace(tzinfo=None)
    cutoff = now - timedelta(hours=max_age_hours)
    for k in list(log.keys()):
        try:
            if datetime.strptime(log[k], "%Y-%m-%d %H:%M") < cutoff:
                del log[k]
        except Exception:
            del log[k]


def should_send_now(state, key, min_interval_minutes):
    """اگر برای این key در min_interval_minutes اخیر پیامی ثبت نشده، True
    برمی‌گرداند و زمان الان را ثبت می‌کند. ورودی‌های قدیمی‌تر از ۱ ساعت
    خودکار حذف می‌شوند تا فایل سنگین نشود."""
    log = state.setdefault("sent_log", {})
    now = tehran_now().replace(tzinfo=None)

    last = log.get(key)
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
            if (now - last_dt) < timedelta(minutes=min_interval_minutes):
                return False
        except Exception:
            pass

    log[key] = now.strftime("%Y-%m-%d %H:%M")
    prune_sent_log(state)
    return True


# ---------------------------------------------------------------------------
# پیام به مالک ربات
# ---------------------------------------------------------------------------
def get_chat(token, chat_id):
    return api_call(token, "getChat", {"chat_id": chat_id})


def set_chat_keypad(token, chat_id, buttons):
    """کیبورد ثابت پایین صفحه رو تنظیم می‌کنه. buttons یه لیست از متن دکمه‌هاست."""
    rows = [{"buttons": [{"id": str(i), "type": "Simple", "button_text": t}]} for i, t in enumerate(buttons)]
    payload = {
        "chat_id": chat_id,
        "chat_keypad_type": "New",
        "chat_keypad": {"rows": rows, "resize_keyboard": True, "one_time_keyboard": False},
    }
    return api_call(token, "editChatKeypad", payload)


# ---------------------------------------------------------------------------
# پخش پیام به همهٔ کانال‌های مقصد
# ---------------------------------------------------------------------------
def broadcast_to_channels(token, config, text):
    sent, failed = 0, 0
    for ch in config.get("destination_channels", []):
        if not ch.get("enabled", True):
            continue
        try:
            send_message(token, ch["guid"], text)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"DEBUG: channelcast failed for {ch.get('name')}: {e}")
    return sent, failed


# ---------------------------------------------------------------------------
# مسدودسازی کاربران
# ---------------------------------------------------------------------------
def is_blocked(state, chat_id):
    """اگه کاربر مسدوده، دیکشنری اطلاعات مسدودی رو برمی‌گردونه؛ وگرنه None.
    اگه تاریخ انقضا گذشته باشه، خودکار از لیست حذف می‌شه."""
    blocked = state.get("blocked_users", {})
    info = blocked.get(chat_id)
    if not info:
        return None
    until = info.get("until")
    if until:
        try:
            until_dt = datetime.strptime(until, "%Y-%m-%d %H:%M")
            if tehran_now().replace(tzinfo=None) >= until_dt:
                del blocked[chat_id]
                return None
        except Exception:
            pass
    return info


def block_user(state, chat_id, reason, days=None):
    blocked_at = tehran_now().strftime("%Y-%m-%d %H:%M")
    until = None
    if days:
        until = (tehran_now().replace(tzinfo=None) + timedelta(days=float(days))).strftime("%Y-%m-%d %H:%M")
    state.setdefault("blocked_users", {})[chat_id] = {
        "reason": reason or "بدون دلیل ذکرشده",
        "blocked_at": blocked_at,
        "until": until,
    }
    return state["blocked_users"][chat_id]


def unblock_user(state, chat_id):
    return state.get("blocked_users", {}).pop(chat_id, None) is not None


def build_blocked_list(state):
    blocked = state.get("blocked_users", {})
    if not blocked:
        return "🚫 هیچ کاربری مسدود نیست."
    lines = [f"🚫 کاربران مسدود ({len(blocked)}):", ""]
    for chat_id, info in blocked.items():
        until = info.get("until") or "دائمی"
        lines.append(f"• {chat_id}\n  دلیل: {info.get('reason')}\n  از: {info.get('blocked_at')} — تا: {until}")
    return "\n".join(lines)


def build_blocked_message(info):
    until = info.get("until") or "دائمی (تا اطلاع ثانوی)"
    return (
        f"⛔ شما توسط مالک ربات مسدود شده‌اید.\n"
        f"دلیل: {info.get('reason')}\n"
        f"تاریخ مسدودیت: {info.get('blocked_at')}\n"
        f"پایان مسدودیت: {until}"
    )


def notify_owner(token, config, text):
    owner = config.get("owner_guid")
    if not owner or owner.startswith("PUT_YOUR"):
        print("DEBUG: notify_owner skipped — owner_guid تنظیم نشده")
        return
    try:
        send_message(token, owner, text)
        print(f"DEBUG: notify_owner ارسال شد به {owner}")
    except Exception as e:
        print(f"DEBUG: notify_owner failed: {e}")


def log_error(state, category, message):
    err = {
        "id": str(uuid.uuid4())[:6],
        "time": tehran_now().strftime("%Y-%m-%d %H:%M"),
        "category": category,
        "message": str(message)[:300],
        "notified": False,
    }
    state.setdefault("errors", []).append(err)
    state["errors"] = state["errors"][-MAX_ERRORS_STORED:]
    print(f"DEBUG ERROR [{category}]: {message}")
    return err


def maybe_notify_new_errors(token, config, state):
    unnotified = [e for e in state.get("errors", []) if not e.get("notified")]
    if not unnotified:
        return
    last_time = state.get("last_error_notify_time")
    now = tehran_now()
    if last_time:
        try:
            last_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M")
            if (now.replace(tzinfo=None) - last_dt) < timedelta(minutes=ERROR_NOTIFY_COOLDOWN_MINUTES):
                return
        except Exception:
            pass
    notify_owner(token, config, f"⚠️ {len(unnotified)} خطای جدید ثبت شد.\nبرای دیدن جزئیات: /bugs")
    for e in unnotified:
        e["notified"] = True
    state["last_error_notify_time"] = now.strftime("%Y-%m-%d %H:%M")


def build_bugs_page(state):
    errors = list(reversed(state.get("errors", [])))
    if not errors:
        return "🎉 هیچ باگی ثبت نشده."
    offset = state.get("bug_page_offset", 0)
    if offset >= len(errors):
        offset = 0
    page = errors[offset: offset + ERRORS_PER_PAGE]
    next_offset = offset + ERRORS_PER_PAGE
    state["bug_page_offset"] = next_offset if next_offset < len(errors) else 0

    by_category = {}
    for e in page:
        by_category.setdefault(e["category"], []).append(e)

    lines = [f"🐞 گزارش باگ‌ها ({offset + 1}-{offset + len(page)} از {len(errors)})"]
    for cat, items in by_category.items():
        lines.append(f"\n📌 دسته: {cat}")
        for e in items:
            lines.append(f"• [{e['time']}] {e['message']}")

    if state["bug_page_offset"] == 0:
        lines.append("\n(به انتهای لیست رسیدید؛ دوباره /bugs بفرستید تا از اول شروع بشه)")
    else:
        lines.append("\nبرای دیدن بعدی، دوباره بنویسید: /bugs")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# پنل مدیریت
# ---------------------------------------------------------------------------
def build_panel_list(config):
    channels = config.get("destination_channels", [])
    if not channels:
        return "هیچ کانال مقصدی در config.json ثبت نشده."
    lines = ["🎛 پنل مدیریت کانال‌ها", ""]
    for i, ch in enumerate(channels, start=1):
        status = "✅ فعال" if ch.get("enabled", True) else "⛔ غیرفعال"
        lines.append(f"{i}. {ch.get('name', ch['guid'])} — {status}")
    lines.append("\nبرای دیدن جزئیات، عدد همون کانال رو بفرستید.")
    return "\n".join(lines)


def build_channel_detail(config, state, index):
    channels = config.get("destination_channels", [])
    if index < 1 or index > len(channels):
        return "همچین شماره‌ای در لیست نیست."
    ch = channels[index - 1]
    guid = ch["guid"]
    posts_count = len(state.get("used_mods_per_channel", {}).get(guid, []))
    activated = state.get("channel_activated", {}).get(guid, "هنوز فعالیتی ثبت نشده")
    status = "✅ فعال" if ch.get("enabled", True) else "⛔ غیرفعال"
    delivery = "مستقیم در کانال" if ch.get("send_file_directly") else "از طریق ربات (پیوی)"
    return (
        f"📊 {ch.get('name', guid)}\n"
        f"وضعیت: {status}\n"
        f"روش تحویل فایل: {delivery}\n"
        f"تعداد پست‌های ارسالی: {posts_count}\n"
        f"فعال از: {activated}"
    )


# ---------------------------------------------------------------------------
# جلوگیری از سنگین‌شدن state.json: مودها/ویدیوهای خیلی قدیمی رو نگه
# نمی‌داریم (فایل‌های واقعی در خودِ روبیکا می‌مونن، فقط از رده‌خارج‌ترین
# ورودی‌های آرشیو داخلی رو کم می‌کنیم).
# ---------------------------------------------------------------------------
def trim_stored_content(state):
    for key, limit in (("mods", MAX_STORED_MODS), ("videos", MAX_STORED_VIDEOS)):
        items = state.get(key, [])
        if len(items) > limit:
            state[key] = items[-limit:]


def track_channel_activation(state, config):
    activated = state.setdefault("channel_activated", {})
    for ch in config.get("destination_channels", []):
        guid = ch["guid"]
        if guid not in activated:
            activated[guid] = tehran_now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# رفع باگ: پیشروی offset حتی وقتی روبیکا next_offset_id خالی برمی‌گردونه
# (وقتی صفحه‌ی آخره). بدون این، last_offset_id هیچ‌وقت جلو نمی‌ره و همون
# پیام‌های قدیمی هر بار از اول پردازش می‌شن.
# ---------------------------------------------------------------------------
def extract_fallback_offset(updates):
    if not updates:
        return None
    last = updates[-1]
    msg = last.get("new_message") or last.get("updated_message") or {}
    return last.get("update_id") or last.get("id") or msg.get("message_id")


# ---------------------------------------------------------------------------
# عضویت اجباری در کانال — بدون متد رسمیِ مستند «چک عضویت» (بر خلاف
# تلگرام). با نام و شکل ورودیِ متدهای مشابه (banChatMember/unbanChatMember
# که chat_id+user_id می‌گیرن) امتحان می‌کنیم. اگه روبیکا چنین متدی
# نداشته باشه، به‌جای مسدود کردن همه‌ی کاربرهای واقعی پشت یه چکِ خراب،
# fail-open می‌کنیم (رد میشن) و به مالک هشدار می‌دیم.
# ---------------------------------------------------------------------------
def check_chat_member(token, channel_guid, user_guid):
    """True/False/None. None یعنی نتونستیم مطمئن چک کنیم."""
    try:
        result = api_call(token, "getChatMember", {"chat_id": channel_guid, "user_id": user_guid})
    except Exception as e:
        print(f"DEBUG: getChatMember ناموفق (channel={channel_guid}, user={user_guid}): {e}")
        return None
    print(f"DEBUG: getChatMember خام: {result}")
    status = None
    if isinstance(result, dict):
        status = result.get("status") or (result.get("member") or {}).get("status") or (result.get("chat_member") or {}).get("status")
    if not status:
        return None
    return str(status).lower() in ("member", "creator", "admin", "administrator", "owner")


def check_all_required_channels(token, config, user_guid):
    """
    (all_joined: bool, missing: list, check_reliable: bool) برمی‌گردونه.
    check_reliable=False یعنی نتونستیم واقعاً چک کنیم (متد جواب معتبر
    نداد) — در این حالت صدازننده باید fail-open رفتار کنه.
    """
    channels = config.get("required_join_channels", [])
    if not channels:
        return True, [], True
    missing = []
    any_unreliable = False
    for ch in channels:
        status = check_chat_member(token, ch["guid"], user_guid)
        if status is None:
            any_unreliable = True
            continue
        if not status:
            missing.append(ch)
    if any_unreliable:
        return (len(missing) == 0), missing, False
    return (len(missing) == 0), missing, True


def build_join_prompt(channels):
    lines = [
        "📣 برای استفاده از ربات باید در کانال های زیر عضو شوید:",
        "",
    ]
    if channels:
        for ch in channels:
            lines.append(str(ch.get("guid", "")).strip())
    else:
        lines.append("هیچ کانالی تنظیم نشده است.")
    lines.extend([
        "",
        "✨️ پس از عضویت در کانال های بالا برای دریافت فایل مود /file را ارسال کنید",
        "⚠️ اگر در چنل های بالا عضو هستید فقط /file را بزنید*"
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# تشخیص اسپم/فعالیت بیش‌ازحد یک کاربر
# ---------------------------------------------------------------------------
SPAM_WINDOW_MINUTES = 5
SPAM_THRESHOLD = 15
SPAM_REPORT_COOLDOWN_MINUTES = 30


def check_spam(state, chat_id):
    now = tehran_now().replace(tzinfo=None)
    log = state.setdefault("activity_log", {})
    entries = log.get(chat_id, [])
    entries.append(now.strftime("%Y-%m-%d %H:%M:%S"))
    cutoff = now - timedelta(minutes=SPAM_WINDOW_MINUTES)
    fresh = []
    for t in entries:
        try:
            if datetime.strptime(t, "%Y-%m-%d %H:%M:%S") >= cutoff:
                fresh.append(t)
        except Exception:
            pass
    log[chat_id] = fresh
    if len(log) > 1000:
        state["activity_log"] = {k: v for k, v in log.items() if v}
    return len(fresh) >= SPAM_THRESHOLD


# ---------------------------------------------------------------------------
# پلن‌های زمان‌بندی پست‌گذاری (قابل تنظیم سراسری یا برای هر کانال)
# ---------------------------------------------------------------------------
DEFAULT_PLANS = {
    "0": {"mod_interval_hours": 2, "video_interval_hours": 4.5},
    "1": {"mod_interval_hours": 1, "video_interval_hours": 3},
    "2": {"mod_interval_hours": 0.5, "video_interval_hours": 2},
}


def resolve_channel_plan(config, channel):
    plans = config.get("schedule", {}).get("plans", DEFAULT_PLANS)
    plan_key = str(channel.get("plan", config.get("schedule", {}).get("default_plan", 1)))
    plan = plans.get(plan_key, DEFAULT_PLANS["1"])
    mod_h = channel.get("mod_interval_hours", plan.get("mod_interval_hours", 1))
    video_h = channel.get("video_interval_hours", plan.get("video_interval_hours", 3))
    return float(mod_h), float(video_h)


# ---------------------------------------------------------------------------
# پخش همگانی
# ---------------------------------------------------------------------------
def broadcast_to_users(token, state, text):
    users = state.get("known_users", [])
    sent, failed = 0, 0
    for uid in users:
        try:
            send_message(token, uid, text)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"DEBUG: broadcast failed for {uid}: {e}")
    return sent, failed


# ---------------------------------------------------------------------------
# پل ارتباطی بین مخزن آرشیور و مخزن ادمین
# ---------------------------------------------------------------------------
MAX_QUEUED_OWNER_COMMANDS = 200


def queue_owner_command(state, chat_id, text):
    """مخزن آرشیور، دستورهای مالک رو خودش اجرا نمی‌کنه — فقط توی این صف
    ذخیره می‌کنه تا مخزن ادمین (که همهٔ منطق دستورها رو داره) بعداً
    بخونه و پردازش کنه."""
    queue = state.setdefault("pending_owner_commands", [])
    queue.append({
        "id": uuid.uuid4().hex[:12],
        "chat_id": chat_id,
        "text": text,
        "time": tehran_now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    if len(queue) > MAX_QUEUED_OWNER_COMMANDS:
        del queue[: len(queue) - MAX_QUEUED_OWNER_COMMANDS]


def fetch_repo_json_file(pat, owner, repo, path, branch="main"):
    """یک فایل JSON رو از یک مخزن گیت‌هاب (حتی خصوصی) با GitHub Contents
    API می‌خونه. pat فقط نیاز به دسترسیِ خواندنِ همون مخزن داره؛ هیچ
    نوشتنی توی مخزن دیگه انجام نمی‌شه."""
    import base64
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return json.loads(content)
