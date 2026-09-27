# ═══════════════════════════════════════════════════════════════
# 🕸️  LUFFY BOT ENGINE  —  10 BOTS + OWNER (NC1→NC10 + FLOOD SHIELD + AI/CONSOLE)
# ═══════════════════════════════════════════════════════════════

import asyncio, os, time, json, random, re, logging
from io import BytesIO
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import OrderedDict, deque
from urllib.parse import quote_plus

import httpx
from telegram import Bot, Update, ChatPermissions
from telegram.error import RetryAfter, Forbidden, BadRequest
from telegram.ext import (
    Application, ApplicationBuilder, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("luffy")

START_TS = time.monotonic()

# ═══════════════════════════════════════════════════════════════
# ⚙️  CONFIG
# ═══════════════════════════════════════════════════════════════

OWNER_ID = 8783373485

BOT_TOKENS = [
    "8693340326:AAExFC74LB1c5E2ChSsxbEOlvAmYq8If7yE",
    "8894673642:AAHRhYNWZt9QBAY91XSnHeRBE0ECni6eCGs",
    "8734293487:AAEz7JrkwWBDttLllFCfd0j3yMAl6wfSDfs",
    "8751764077:AAGy0A-KYiPRkPXm2h4XABc7-pgwR6wmNGQ",
    "8711820262:AAEg-WloDSXCp3ofrB1i9nLdGePQFMan_lM",
    "8657017426:AAGA3ScARnus0sgVtMXLfxZr4k4xJ_IY8Uk",
    "8897267565:AAGkr3WXX7c_oE0JoCaZIs5fcnDKZV5mRFw",
    "8843077373:AAGM1dXVUl48MbLTsy1xA_W91Z2XU73yM1E",
    "8906240267:AAEpKedSvpEUAwVHp1TSWpamB2Mm5SfzdIQ",
    "8653355342:AAGZTLP3BAnxKQoqLaW_u5KO2qqbHTMPmok",
]

PREFIX        = "~"
IST           = timezone(timedelta(hours=5, minutes=30))
REPLY_PREFIX  = "⸎❄️ "
DEFAULT_DELAY = 0.0

BOT_TOKENS = [t.strip() for t in BOT_TOKENS
              if t.strip() and t.strip() != "ADD TOKEN HERE"]
if not BOT_TOKENS:
    raise SystemExit("❌ Koi valid token nahi mila.")

log.info(f"🔑 Loaded {len(BOT_TOKENS)} token(s)")
log.info(f"👤 OWNER_ID = {OWNER_ID}")

def prefixed(text: str) -> str:
    return f"{REPLY_PREFIX}{text}"


# ═══════════════════════════════════════════════════════════════
# 💾  STORAGE
# ═══════════════════════════════════════════════════════════════
SUDO_FILE  = Path("sudo.json")
DELAY_FILE = Path("delays.json")
HIST_FILE  = Path("userhistory.json")

def _read(p, default):
    try: return json.loads(p.read_text()) if p.exists() else default
    except Exception: return default

def _write(p, data):
    try: p.write_text(json.dumps(data, default=str))
    except Exception: pass

SUDO_USERS = set(_read(SUDO_FILE, []))
CHAT_DELAY = {int(k): float(v) for k, v in _read(DELAY_FILE, {}).items()}
USER_HIST  = _read(HIST_FILE, {})

def save_sudo():   _write(SUDO_FILE,  list(SUDO_USERS))
def save_delays(): _write(DELAY_FILE, {str(k): v for k, v in CHAT_DELAY.items()})
def save_userhist(): _write(HIST_FILE, USER_HIST)

def get_delay(chat_id: int) -> float:
    return CHAT_DELAY.get(chat_id, DEFAULT_DELAY)

def is_authorized(uid: int) -> bool:
    return uid == OWNER_ID or uid in SUDO_USERS


def track_user(user):
    if not user: return
    uid = str(user.id)
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    rec = USER_HIST.setdefault(uid, {
        "names": [], "usernames": [],
        "first_seen": now, "last_seen": now
    })
    rec["last_seen"] = now
    full = (user.first_name or "")
    if getattr(user, "last_name", None):
        full += " " + user.last_name
    full = full.strip()
    if full and (not rec["names"] or rec["names"][-1] != full):
        rec["names"].append(full); rec["names"] = rec["names"][-10:]
    un = getattr(user, "username", "") or ""
    if un and (not rec["usernames"] or rec["usernames"][-1] != un):
        rec["usernames"].append(un); rec["usernames"] = rec["usernames"][-10:]
    save_userhist()


# ═══════════════════════════════════════════════════════════════
# 🚀  ADVANCED FLOOD BYPASS — TOKEN BUCKET + ADAPTIVE BACKOFF
# ═══════════════════════════════════════════════════════════════

class RateGuard:
    def __init__(self, bot_id: int, rate: float = 28.0,
                 burst: int = 30, max_conc: int = 4):
        self.bot_id      = bot_id
        self.base_rate   = rate
        self.rate        = rate
        self.capacity    = float(burst)
        self.tokens      = float(burst)
        self.last_refill = time.monotonic()

        self._sem        = asyncio.Semaphore(max_conc)
        self._lk         = asyncio.Lock()

        self._flood_until = 0.0
        self._penalty     = 0.0
        self._errors      = deque(maxlen=32)
        self._rng         = random.Random(bot_id ^ 0xC0FFEE)

        self._INVIS = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
                       "\u00ad", "\u034f", "\u17b5", "\u180e", "\u3164"]

    async def _gate(self):
        async with self._lk:
            now = time.monotonic()
            self.tokens = min(
                self.capacity,
                self.tokens + (now - self.last_refill) * self.rate
            )
            self.last_refill = now

            wait = 0.0
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) / self.rate

            now2 = time.monotonic()
            if self._flood_until > now2:
                wait = max(wait, self._flood_until - now2)

            if wait > 0:
                await asyncio.sleep(wait + self._rng.uniform(0.0, 0.02))
                self.last_refill = time.monotonic()

            self.tokens = max(0.0, self.tokens - 1.0)

    def _on_flood(self, retry_after: float):
        now    = time.monotonic()
        capped = min(float(retry_after), 8.0)
        self._flood_until = now + capped
        self._penalty     = min(2.0, self._penalty * 1.5 + 0.05)
        self._errors.append(now)

        recent = [t for t in self._errors if now - t < 30]
        if len(recent) >= 4:
            self.rate   = max(6.0, self.rate * 0.85)
            self.tokens = min(self.tokens, 2.0)
        else:
            self.rate = min(self.base_rate, self.rate + 0.5)

    def _on_success(self):
        if self._penalty > 0:
            self._penalty *= 0.98

    async def send_message(self, bot, chat_id, text, **kw):
        async with self._sem:
            await self._gate()
            for _ in range(5):
                try:
                    r = await bot.send_message(chat_id, text, **kw)
                    self._on_success()
                    return r
                except RetryAfter as e:
                    ra = float(getattr(e, "retry_after", 1.0))
                    self._on_flood(ra)
                    await asyncio.sleep(ra + self._rng.uniform(0.05, 0.4))
                except (Forbidden, BadRequest):
                    return None
                except Exception:
                    await asyncio.sleep(0.05 + self._rng.uniform(0, 0.05))
            return None

    async def set_chat_title(self, bot, chat_id, title):
        async with self._sem:
            await self._gate()
            for _ in range(5):
                try:
                    r = await bot.set_chat_title(chat_id, title[:128])
                    self._on_success()
                    return r
                except RetryAfter as e:
                    ra = float(getattr(e, "retry_after", 1.0))
                    self._on_flood(ra)
                    await asyncio.sleep(ra + self._rng.uniform(0.05, 0.4))
                except (Forbidden, BadRequest):
                    return None
                except Exception:
                    await asyncio.sleep(0.05)
            return None

    async def set_chat_photo(self, bot, chat_id, photo):
        async with self._sem:
            await self._gate()
            for _ in range(5):
                try:
                    r = await bot.set_chat_photo(chat_id, photo=photo)
                    self._on_success()
                    return r
                except RetryAfter as e:
                    ra = float(getattr(e, "retry_after", 1.0))
                    self._on_flood(ra)
                    await asyncio.sleep(ra + self._rng.uniform(0.05, 0.4))
                except (Forbidden, BadRequest):
                    return None
                except Exception:
                    await asyncio.sleep(0.05)
            return None

    async def send_photo(self, bot, chat_id, photo, **kw):
        async with self._sem:
            await self._gate()
            for _ in range(5):
                try:
                    r = await bot.send_photo(chat_id, photo, **kw)
                    self._on_success()
                    return r
                except RetryAfter as e:
                    ra = float(getattr(e, "retry_after", 1.0))
                    self._on_flood(ra)
                    await asyncio.sleep(ra + self._rng.uniform(0.05, 0.4))
                except (Forbidden, BadRequest):
                    return None
                except Exception:
                    await asyncio.sleep(0.05)
            return None

    async def set_message_reaction(self, bot, **kw):
        async with self._sem:
            await self._gate()
            try:
                r = await bot.set_message_reaction(**kw)
                self._on_success()
                return r
            except RetryAfter as e:
                self._on_flood(float(getattr(e, "retry_after", 1.0)))
                return None
            except Exception:
                return None

    def suffix(self, i: int) -> str:
        return self._INVIS[i % len(self._INVIS)]

    @property
    def delay(self) -> float:
        return max(0.0, self._flood_until - time.monotonic())


_guards: dict = {}

def get_limiter(bot_id: int) -> RateGuard:
    g = _guards.get(bot_id)
    if g is None:
        g = RateGuard(bot_id)
        _guards[bot_id] = g
    return g


# ═══════════════════════════════════════════════════════════════
# 🧠  STATE
# ═══════════════════════════════════════════════════════════════
ALL_APPS: list[Application] = []
ALL_BOTS: list[Bot] = []
BOT_INFO: dict = {}
TASKS:    dict = {}

SWIPE_ON:   dict = {}
SWIPE_MSG:  dict = {}
SWIPE_SRC:  dict = {}
CHUD_TEXT:  dict = {}
AR_TARGET:  dict = {}
OVER_MEDIA: dict = {}
MENU_MEDIA: dict = {}

TARGETS: dict = {}   # chat_id -> {uid: name}
HYPER:   dict = {}   # chat_id -> {"on": bool, "prev": dict|None}

spamwatch_chats: dict = {}
_sw_bursts: dict = {}
SW_WINDOW  = 4
SW_THRESH  = 6
SW_COUNTER = "luffygod"

_handled: "OrderedDict" = OrderedDict()
_HANDLED_CAP = 5000

def add_task(chat_id, task, kind="generic"):
    task._kind = kind
    TASKS.setdefault(chat_id, []).append(task)

def cancel_tasks(chat_id, kinds=None):
    if chat_id not in TASKS: return
    keep = []
    for t in TASKS[chat_id]:
        k = getattr(t, "_kind", "generic")
        if kinds is None or k in kinds: t.cancel()
        else: keep.append(t)
    TASKS[chat_id] = keep


# ═══════════════════════════════════════════════════════════════
# 🔤  FANCY FONT
# ═══════════════════════════════════════════════════════════════
_BOLD = {
    'a':'𝐀','b':'𝐁','c':'𝐂','d':'𝐃','e':'𝐄','f':'𝐅','g':'𝐆','h':'𝐇','i':'𝐈',
    'j':'𝐉','k':'𝐊','l':'𝐋','m':'𝐌','n':'𝐍','o':'𝐎','p':'𝐏','q':'𝐐','r':'𝐑',
    's':'𝐒','t':'𝐓','u':'𝐔','v':'𝐕','w':'𝐖','x':'𝐗','y':'𝐘','z':'𝐙',
}
_SMALL = {
    'a':'ᴀ','b':'ʙ','c':'ᴄ','d':'ᴅ','e':'ᴇ','f':'ғ','g':'ɢ','h':'ʜ','i':'ɪ',
    'j':'ᴊ','k':'ᴋ','l':'ʟ','m':'ᴍ','n':'ɴ','o':'ᴏ','p':'ᴘ','q':'ǫ','r':'ʀ',
    's':'s','t':'ᴛ','u':'ᴜ','v':'ᴠ','w':'ᴡ','x':'x','y':'ʏ','z':'ᴢ',
}

def fancy(text: str) -> str:
    out, new_word = [], True
    for ch in text:
        low = ch.lower() if ch.isalpha() else None
        if low is not None and low in _BOLD:
            out.append(_BOLD[low] if new_word else _SMALL[low])
            new_word = False
        else:
            out.append(ch)
            if ch in " \t\n._-|([{": new_word = True
    return "".join(out)


# ═══════════════════════════════════════════════════════════════
# 🎯  MODES & TEMPLATES
# ═══════════════════════════════════════════════════════════════
MSG_MODES   = {"msg","spam","chud","gearspam"}
TITLE_MODES = {"title","nc","nc1","nc2","nc3","nc4","nc5",
               "nc6","nc7","nc8","nc9","nc10",
               "slutnc","kaidonc","ncblast","blastnc","pilla","gulam","hasy",
               "luffygod","filternc","vegetagawd","glitchnc","glitch","cutenc"}
BOTH_MODES  = {"both","luffygod","blast","gearspam"}
FAST_MODES  = {"both","title","nc","luffygod"}

emox_EMOJIS = ["🔥","⚡","💀","🖤","👑","🕸️","🥀","⚔️","🩸","🌪️","🎭","🔮","⛓️","💣",
               "🦇","🪓","👁️","🛸","❄️","🪐"]

BLASTNC_EMOJI = ["🫪","🔥","⚡","💀","🖤","👑","🕸️","🥀","⚔️","🩸","🌪️","🎭","🔮",
                 "⛓️","💣","🦇","🪓","👁️","🛸","❄️","🪐"]

PILLA_EMOJI   = ["🍌","🥴","💞","🌸","🍋","💎","🍄","🚀","🐬","🥑","🎈","🧿","🍕",
                 "🦜","🌵","🎸","🍭","🌊","🧩","🦁","🍦","🔮","⚡"]

GULAM_EMOJI   = ["🇮🇳","🇺🇸","🇯🇵","🇧🇷","🇩🇪","🇫🇷","🇬🇧","🇨🇦","🇦🇺","🇷🇺","🇮🇹",
                 "🇰🇷","🇪🇸","🇲🇽","🇦🇷","🇹🇷","🇸🇦","🇪🇬","🇿🇦","🇳🇿","🇨🇭","🇸🇪",
                 "🇳🇱","🇦🇪","🇮🇩","🇵🇹","🇬🇷","🇳🇴","🇵🇭","🇹🇭"]

HASY_EMOJI    = ["❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔","❤️‍🔥","❤️‍🩹",
                 "💖","💗","💓","💞","💕","💘","💝","💟"]

LUFFYGOD_EMOJI = ["🌑","🌒","🌓","🌔","🌕","🌖","🌗","🌘","🌙","🌚","🌛","🌜","🌝",
                  "🌊","🪸","🐚","🏝️","⚓","⛵","🚤","🛳️","🚢","🐋","🐳","🐬","🦈",
                  "🐙","🦑","🦐","🦞","🦀","🐡","🐠","🐟","🪼"]

FILTER_EMOJI  = ["❤️","🧡","💛","💚","💙","💜","🖤"]

VEGETA_EMOJI  = ["🧿","🗿","🪬","🪩","🧬","🪆","🪶","🪐","🪔","🪞","🪚","🪤","🪄",
                 "🪫","🪡"]

NC2_EMOJI     = ["👍","👎","❤️","🔥","🥰","👏","😁","🤔","🤯","😱","🤬","😢","🎉",
                 "🤩","🤮","💩","🙏","👌","🕊️","🤡","🥱","🥴","😍","🐳","❤️‍🔥","🌚",
                 "🌭","💯","🤣","⚡","🍌","🏆","💔","🤨","😐","🍓","🍾","💋","🖕",
                 "😈","😴","😭","🤓","👻","👨‍💻","👀","🎃","🙈","😇","😨","🤝","✍️",
                 "🤗","🫡","🎅","🎄","☃️","💅","🗿"]

NC3_EMOJI     = ["😀","😃","😄","😁","😆","😅","🤣","😂","🙂","🙃","😉","😊","😇",
                 "🥰","😍","🤩","😘","😗","😚","😙","😋","😛","😜","🤪","😝","🤑",
                 "🤗","🫣","🤭","🫢","🫡","🤫","🫠","🤔","🤐","🤨","😐","😑","😶",
                 "🫥","😏","😒","🙄","😬","😮‍💨","🤥","🫨","😌","😔","😪","🤤","😴",
                 "😷","🤒","🤕","🤢","🤮","🤧","🥵","🥶","🥴","😵","😵‍💫","🤯","🤠",
                 "🥳","🥸","😎","🤓","🧐","😕","🫤","😟","🙁","☹️","😮","😯","😲",
                 "😳","🥺","🥹","😦","😧","😨","😰","😥","😢","😭","😱","😖","😣",
                 "😞","😓","😩","😫","🥱","😤","😡","😠","🤬","😈","👿","💀","☠️",
                 "💩","🤡","👹","👺","👻","👽","👾","🤖","😺","😸","😹","😻","😼",
                 "😽","🙀","😿","😾"]

NC4_EMOJI     = ["🌸","💮","🏵️","🌹","🥀","🌺","🌻","🌼","🌷","🪷","💐"]

NC5_EMOJI     = ["❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","🩷","🩵","🩶","💔",
                 "❤️‍🔥","❤️‍🩹","❣️","💕","💞","💓","💗","💖","💘","💝","💟","🫀",
                 "💌","🏩","💒","💑","👩‍❤️‍👨","👨‍❤️‍👨","👩‍❤️‍👩","💏",
                 "👩‍❤️‍💋‍👨","👨‍❤️‍💋‍👨","👩‍❤️‍💋‍👩","🫶","😻","🫰","🫳","🫴"]

NC6_EMOJI = ["💐","🌸","🪴","🪷","🪻","🌼","🌷","🌹","🌺","🌻",
             "🍏","🍋","🍎","🍋‍🟩","🍐","🍊","🍈","🫐","🍉","🍓","🍇",
             "🍒","🍑","🥭","🍍","🍆","🍅","🥬","🥦","🥝","🫛","🥥","🥑",
             "🌶️","🫑"]

NC7_EMOJI = ["🪬","🧿","🪞","🪆","🪩","🗿","🪦","🪚","🪓","🪤","🪱","🪰",
             "🪲","🪳","🪼","🪸","🍄‍🟫","🪷","🫧","🪐","🪫","🔌","🧭","🕹️",
             "🪩","🧬","🧪","🧫","🔮","🧿","👁️‍🗨️","🗝️","🪄","🪶","🫀","🫁",
             "🪠","🧯","🧱","⛓️"]

NC8_EMOJI = ["✦","✧","★","☆","✬","✭","✮","✯","✰","✵","✶","✷",
             "✸","✹","❂","✺","ᯓ","⋆","｡","°","☾","☽","☼",
             "𓆩♡𓆪","𖦹","꩜","𖠋"]

NC9_EMOJI = ["🕛","🕧","🕐","🕜","🕑","🕝","🕒","🕞","🕓","🕟",
             "🕔","🕠","🕕","🕡","🕖","🕢","🕗","🕣","🕘","🕤",
             "🕙","🕥","🕚","🕦","⏰","⏱️","⏲️","⏳","⌛","🕰️"]

NC10_EMOJI = ["🥷","🤺","👺","👹","👻","💀","☠️","🤖","👽","👾",
              "🧙‍♂️","🧙‍♀️","🧚‍♂️","🧚‍♀️","🧛‍♂️","🧛‍♀️","🧜‍♂️","🧜‍♀️",
              "🧝‍♂️","🧝‍♀️","🧞‍♂️","🧞‍♀️","🧟‍♂️","🧟‍♀️",
              "⚡","🔥","💥","✨","🌟","💫","🍙","🍜","🍡","🍱","🍣",
              "🍛","🍥","🍵","🍶","🥢","🌸","💮","🎏","🎐","🎋","🏮",
              "⛩️","🎎","🗻","👘","🥋","🥊","🗡️","⚔️","🏹","🛡️","🔮",
              "🪄","🧿","📜","🎮","🕹️","🎧","🖤","❤️‍🔥","👁️","🩸","💢","💨","💤"]

WORDS_SLUTNC = ["LUND","TBKC","TBR","TMR","aarey चुदोड़े","BHEN CUDALE"]

GLITCH_WORDS = ["𝐆ᴜʟᴀᴍ","𝐑ᴀɴᴅɪᴋᴇ","हिजड़ा","LUND CHOOS","CHUD AB","TMKC","TMR",
                "TBKC","CVR KRR"]

CUTENC_POOL = [
    "{text}◦𓂃ːBʜꪮsᴅꫀ´-⋆͙🎀",
    "{text}◦𓂃ːCʜɪɴꪖꪶ sꪮɴ´-⋆͙🎀",
    "{text}◦𓂃ːRꫝɴᴅɪ ʙꪮɪ´-⋆͙🎀",
    "{text}◦𓂃ːSꪊᴀʀ´-⋆͙🎀",
    "{text}◦𓂃ːLꫝɴɢᴅꫀ ʟꪊᴅꪮ´-⋆͙🎀",
    "{text}◦𓂃ːCʜꪊᴅꫝʏɪ ᴋʜꫝɴꫀ´-⋆͙🎀",
    "{text}◦𓂃ːBʜꪮsᴅɪᴋᴀ´-⋆͙🎀",
    "{text}◦𓂃ːLꪊɴᴅ ᴘꫀ´-⋆͙🎀",
    "{text}◦𓂃ːKꪊᴛᴛɪʏꫝ´-⋆͙🎀",
    "{text}𓂃ːCʜꪊᴅ ɢʏꪖ´-⋆͙🎀",
    "{text}◦𓂃ːMꪮꪮᴛ ᴘɪ´-⋆͙🎀",
    "{text}◦𓂃ːBꫀᴛɪ ᴛᴍᴋᥴ´-⋆͙🎀",
]

KAIDONC_TPL = [
    "{text} Hᴇʏ Mғ I'ʟʟ Fᴜᴄᴋ Yᴏᴜʀ Mᴏᴍ Sᴏ Hᴀʀᴅ Tʜᴀᴛ Sʜᴇ Cᴀɴᴛ Eᴠᴇɴ Wᴀᴋᴇ Uᴘ ❕❔",
    "{text} Sʟᴜᴛ Gᴏ Asᴋ Uʀ Mᴏᴍ Wʜᴏ Is Yᴏᴜʀ Rᴇᴀʟ Dᴀᴅ❕❔",
    "{text} Yᴏᴜʀ Mᴏᴍᴍᴀ Gᴏᴛ A Gᴏᴏғʏ Gɪᴀɴᴛ Ass Lᴍᴀᴏ❕❔",
    "{text} Iᴍ Sᴏʀʀʏ Bᴜᴛ Yᴏᴜʀ Mᴏᴍ Dɪᴇᴅ Wʜɪʟᴇ Fᴜᴄᴋɪɴɢ Wɪᴛʜ Mᴇ❕❔",
    "{text} Hᴇʏ Sᴏɴ Oғ A Bɪᴛᴄʜ Gᴏ Cᴀʟʟ Yᴏᴜʀ Sɪs I Wᴀɴᴛ Tᴏ Fᴜᴄᴋ Hᴇʀ Bᴀᴅʟʏ❕❔",
]

NC1_TPL = [
    "𓆩😂𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😂𓆪ྀ࿐",
    "𓆩😁𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😁𓆪ྀ࿐",
    "𓆩😭𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😭𓆪ྀ࿐",
    "𓆩😘𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😘𓆪ྀ࿐",
    "𓆩🥰𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🥰𓆪ྀ࿐",
    "𓆩😍𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😍𓆪ྀ࿐",
    "𓆩🥳𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🥳𓆪ྀ࿐",
    "𓆩☺️𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩☺️𓆪ྀ࿐",
    "𓆩🤤𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🤤𓆪ྀ࿐",
    "𓆩😋𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😋𓆪ྀ࿐",
    "𓆩😝𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😝𓆪ྀ࿐",
    "𓆩😔𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😔𓆪ྀ࿐",
    "𓆩🥺𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🥺𓆪ྀ࿐",
    "𓆩🤭𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🤭𓆪ྀ࿐",
    "𓆩🥱𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🥱𓆪ྀ࿐",
    "𓆩😱𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😱𓆪ྀ࿐",
    "𓆩🤗𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🤗𓆪ྀ࿐",
    "𓆩😤𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😤𓆪ྀ࿐",
    "𓆩😮‍💨𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😮‍💨𓆪ྀ࿐",
    "𓆩😡𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😡𓆪ྀ࿐",
    "𓆩😳𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😳𓆪ྀ࿐",
    "𓆩🤮𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🤮𓆪ྀ࿐",
    "𓆩😇𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩😇𓆪ྀ࿐",
    "𓆩🤡𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🤡𓆪ྀ࿐",
    "𓆩💀𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩💀𓆪ྀ࿐",
    "𓆩👹𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩👹𓆪ྀ࿐",
    "𓆩👺𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩👺𓆪ྀ࿐",
    "𓆩👾𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩👾𓆪ྀ࿐",
    "𓆩🌚𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🌚𓆪ྀ࿐",
    "𓆩💢𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩💢𓆪ྀ࿐",
    "𓆩🕸️𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩🕸️𓆪ྀ࿐",
    "𓆩⭕️𓆪 {text} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 𝘾𝙃𝙐𝘿𝙄 𓆩⭕️𓆪ྀ࿐",
]

NC6_TPL  = "×{emoji}×{text} ꜱᴀʏ ʟᴜꜰꜰʏ ᴅᴀᴅᴅʏ ×{emoji}×"

NC7_TPL  = ("*{emoji}* {text} KAMZOR PILLA "
            + ("⟲ ⟳ "*40)
            + "{emoji} ⟳ ⟲ ⟳ ⟲ ⟳ ⟲ ⟳ ⟲ ⟳ ⟲ ⟳ ⟲ ⟳ ⟲ "
            + "{emoji}")

NC8_TPL  = ("⧼{emoji}⧽{text} LUND PE AAKE NAACH 🩷🩵⭕️🤍💚🩶🖤❤️💙 ꧁, ꧂ 🤎🧡💜💛🩷🩵 ꧁, ꧂ 🤍💚⭕️🩶🖤❤️⭕️💙🤎🧡💜⭕️💛🩷🩵🤍💚🩶🖤❤️ ꧁, ꧂ 💙🤎🧡⭕️💜⭕️ ꧁, ꧂ 💛🩷⭕️🩵🤍💚⭕️🩶🖤❤️💙🤎🧡💜💛🩷🩵 ꧁, ꧂ "*8)

NC9_TPL  = ("<{emoji}>︵‿₊୨{text}୧₊‿︵‧KUTTIYA "
            + ("𒈙𒈔𒈙𒈔𒈙𒈔🔴" * 40))

NC10_TPL = ("☣️───《{text}》‧︵‿{emoji}‿︵‧ ☬ PILLO KI T⭕️LI 🥀 "
            + ("𒐫" * 64)
            + "《🤣》───☣️ 🤣{emoji}🤣")

MODE_MAP = {
    "spam":      ["{text} 𝗧𝗘𝗥𝗜 𝗕𝗔𝗛𝗘𝗡 𝗞𝗘 𝗦𝗔𝗔𝗧 𝗦3𝗫 𝗞𝗥𝗨 𝗚𝗔 𝗕𝗛𝗔𝗔𝗚 𝗠𝗧 𝗠𝗔𝗗𝗥𝗖𝗛⭕️𝗗 🤍🌙🕊️_____________________/"*25],
    "msg":       ["{text} 💀"],
    "gearspam":  ["⮑{target}⮐ #γє #ριℓℓE #gαяєєϐ #мαα #κι #gαяєєϐ #ϲнυτ #ѕє #иικℓE #нυE #κєє∂E #нαι⇕ ⇖ ⇗ ⇘ ⇙ ⇕ ⇖ ⇗ ⇘ ⇙ ⇕ ⇖ ⇗ ⇘ ⇙ ⇕ ⇖ ⇗⇕ ⇖ ⇗ ⇘ ⇙ ⇕ ⇖ ⇗ ⇘ ⇙ ⇕ ⇖ ⇗ ⇘ ⇙ ⇕ ⇖ [PILL⭕️ KI ~ T⭕️LI] "
                  ". ݁₊ ⊹ . ݁˖ . ݁𓂃"*10],
    "chud":      ["{text} Tᴇʀɪ Dᴀᴅɪ ᴋ ᴄʜᴍᴜᴛ ᴍᴀɪ ᴛᴀɴɢ ᴀᴛᴋᴀ ᴅᴜɴɢᴀ 😍"],

    "title":     ["{text} RΛɴᴅʏᴋᴇ LΛᴅᴋᴇ 🔥 " + "𒐫"*40],
    "nc":        ["{text} RΛɴᴅʏᴋᴇ LΛᴅᴋᴇ 🔥 " + "𒐫"*40],
    "slutnc":    ["{text} {word} 𒐫𒐫𒐫❌𒐫❌𒐫🩷𒐫𒐫✳️❌ 𒐫🩷𒐫𒐫✅𒐫❌𒐫𒐫🩷𒐫🩷 ➴ྀ࿐ 𒐫𒐫𒐫❌𒐫❌𒐫𒐫𒐫✳️❌ 𒐫𒐫𒐫✅𒐫❌𒐫𒐫𒐫 ➴ྀ࿐ 𒐫🩷𒐫𒐫❌𒐫❌𒐫𒐫𒐫✳️❌ 𒐫🩷𒐫𒐫✅𒐫❌𒐫🩷𒐫𒐫 ➴ྀ࿐ ··"],
    "kaidonc":   KAIDONC_TPL,
    "ncblast":   ["{text} RΛɴᴅʏᴋᴇ LΛᴅᴋᴇ {emoji} " + "𒐫"*54],
    "blastnc":   ["{text} RΛɴᴅʏᴋᴇ LΛᴅᴋᴇ {emoji} " + "𒐫"*54],
    "pilla":     ["{text} 𝐓ᴇʀ𝐈 𝐌ᴀ𝐀 𝐂ʜ⭕𝐃 𝐃△ʟᴇɴɢ𝐄 <{emoji}>"],
    "gulam":     ["[{emoji}] {text} GULAM CHUD " + "❌⭕️"*30],
    "hasy":      ["{text} ᑕʜᴜƿ RΛɴᴅʏᴋΣ" + "𓍯😂"*22 + "〘{emoji}〙"],
    "luffygod":  ["𓆩{emoji}𓆪 {text} ʟᴀɢᴀ ᴛᴇʀʏ ᴍᴀᴀ ᴋɪ ᴄʜᴏᴏᴛ ᴋᴀ ᴅᴀᴍᴍ "
                  + "𓆩ᥫ᭡𓆪⭕️"*70 + " 𝗧ᴍᴋᴄ 🅜🅐🅘 𝗞ᴇʟᴀ 🛕🍌"],
    "filternc":  ["{text} 𝙈𝘼𝘿𝘼𝙍𝘾𝙃⭕𝘿" + "𒐫𒐫𒐫{emoji}"*23],
    "vegetagawd":["『{emoji}> {text} 𝙖𝙖𝙜 𝙡𝙖𝙜𝙞 𝙝 𝙗𝙖𝙨𝙩𝙞 𝙢 𝙩𝙚𝙧𝙞 𝙢𝙖𝙖 𝙘𝙝𝙪𝙙𝙧𝙞 𝙢𝙖𝙨𝙩𝙞 𝙢 "
                  "🍷━━━》💜🩷💚💛🤍🩷💜❤️🖤💛🩷💜🧡🖤❤️🤎🧡🖤🩷💜🤍🩷💜❤️🖤💛🤍🩷💜🩶💛❤️🤍🩷💜🖤💛💜🩷💛🩶💜🧡🖤🩷💜🧡🖤🩷💜🧡🖤💙🩷💜❤️💚🧡🩷💙🧡🖤🩷💙❤️🖤💛🩷💜💛🖤❤️━━━》🍷━━》━━━━━━》🍷━━━━》🍷━━━━━"],
    "glitchnc":  ["{text} {word} 🐷 " + "𒈔𒈔𒈔🩷"*20],
    "glitch":    ["{text} {word} 🐷 " + "𒈔𒈔𒈔🩷"*20],
    "cutenc":    ["{cut} {text}"],
    "nc1":       NC1_TPL,
    "nc2":       ["{emoji} {text} ᑕᕼᑌᑭ ᗰᗩᴋᴇᒪᴏᗪᴇ " + "𒈙✳️"*25],
    "nc3":       ["<{emoji}> {text} " + "𒅄𒅄𒅄🍌𒅄𒅄𒅄𒅄🍷"*10],
    "nc4":       ["({emoji})‧︵‿₊୨{text}୧₊‿︵‧{text}₊⊹ 𝐓ᴇʀɪ 𝐌ᴀᴀ सूरज ☀️ "
                  "𝐍ɪᴋᴀʟɴᴇ 𝐒ᴇ 𝐏ᴀʜʟᴇ 𝐇ᴇ 𝐂ʜᴏᴅ 𝐃ᴜ 👈 🟡 ({emoji})"],
    "nc5":       ["<{text}>Cʜꪊᴅ ɢʏꪖ"+"𒅒❤️"*48],
    "nc6":       [NC6_TPL],
    "nc7":       [NC7_TPL],
    "nc8":       [NC8_TPL],
    "nc9":       [NC9_TPL],
    "nc10":      [NC10_TPL],
    "both":      ["{text} SAY LUFFY DADDY STOP!! " + "𒐫"*30],
    "blast":     ["{text} RΛɴᴅʏᴋᴇ LΛᴅᴋᴇ 💣 " + "𒐫"*40],
}

photo_pool: list[bytes] = []


# ═══════════════════════════════════════════════════════════════
# 🔁  RENDER + LOOPS
# ═══════════════════════════════════════════════════════════════
def _render(template: str, base: str, idx: int) -> str:
    out = template.replace("{text}", base)

    if "{emoji}" in out:
        pool = BLASTNC_EMOJI
        if "GULAM" in out or ("CHUD" in out and "❌⭕️" in out):
            pool = GULAM_EMOJI
        elif "ᑕʜᴜƿ" in out:
            pool = HASY_EMOJI
        elif "𓆩" in out and "ʟᴀɢᴀ" in out:
            pool = LUFFYGOD_EMOJI
        elif "𝙈𝘼𝘿𝘼𝙍𝘾𝙃" in out:
            pool = FILTER_EMOJI
        elif "『" in out and "𝙖𝙖𝙜" in out:
            pool = VEGETA_EMOJI
        elif "ᑕᕼᑌᑭ" in out:
            pool = NC2_EMOJI
        elif "𒅄" in out:
            pool = NC3_EMOJI
        elif "सूरज" in out:
            pool = NC4_EMOJI
        elif "𒅒" in out:
            pool = NC5_EMOJI
        elif "𝐓ᴇʀ𝐈" in out:
            pool = PILLA_EMOJI
        elif "ꜱᴀʏ ʟᴜꜰꜰʏ ᴅᴀᴅᴅʏ" in out:
            pool = NC6_EMOJI
        elif "KAMZOR PILLA" in out:
            pool = NC7_EMOJI
        elif "LUND PE AAKE NAACH" in out:
            pool = NC8_EMOJI
        elif "KUTTIYA" in out:
            pool = NC9_EMOJI
        elif "PILLO KI T" in out:
            pool = NC10_EMOJI
        out = out.replace("{emoji}", pool[idx % len(pool)])

    if "{word}" in out:
        pool = WORDS_SLUTNC if "𒐫" in out else GLITCH_WORDS
        out = out.replace("{word}", pool[idx % len(pool)])

    if "{cut}" in out:
        out = out.replace("{cut}", CUTENC_POOL[idx % len(CUTENC_POOL)])

    return out


async def bot_loop(bot: Bot, bot_id: int, chat_id: int,
                   base: str, mode: str, reply_to_id=None):
    guard     = get_limiter(bot_id)
    templates = MODE_MAP.get(mode, emox_EMOJIS)
    i, tick   = 0, 0

    while True:
        try:
            raw = templates[i % len(templates)]
            txt = _render(raw, base, i) + guard.suffix(i)

            if mode in FAST_MODES:
                tick += 1
                if tick % 12 == 0:
                    try: await bot.get_chat(chat_id)
                    except Exception: pass

            if mode in BOTH_MODES:
                await asyncio.gather(
                    guard.send_message(bot, chat_id, txt,
                                       reply_to_message_id=reply_to_id),
                    guard.set_chat_title(bot, chat_id, txt),
                    return_exceptions=True,
                )
            elif mode in TITLE_MODES:
                await guard.set_chat_title(bot, chat_id, txt)
            else:
                await guard.send_message(bot, chat_id, txt,
                                         reply_to_message_id=reply_to_id)

            i += 1
            d = get_delay(chat_id)
            if d > 0:
                await asyncio.sleep(d)
            else:
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(0.05)


async def photo_loop(bot: Bot, bot_id: int, chat_id: int):
    guard, i = get_limiter(bot_id), 0
    while True:
        try:
            if not photo_pool:
                await asyncio.sleep(0.5); continue
            await guard.set_chat_photo(
                bot, chat_id, BytesIO(photo_pool[i % len(photo_pool)])
            )
            i += 1
            d = get_delay(chat_id)
            if d > 0: await asyncio.sleep(d)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(0.3)


async def swipe_loop(bot: Bot, bot_id: int, chat_id: int,
                     text: str, reply_to_id=None):
    guard = get_limiter(bot_id)
    i     = 0
    while SWIPE_ON.get(chat_id, False):
        try:
            await guard.send_message(
                bot, chat_id, text + guard.suffix(i),
                reply_to_message_id=reply_to_id)
            i += 1
            d = get_delay(chat_id)
            if d > 0:
                await asyncio.sleep(d)
            else:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(0.05)


async def promote_all_bots(chat_id: int) -> list[str]:
    promoter = None
    for app in ALL_APPS:
        try:
            me = await app.bot.get_chat_member(chat_id, app.bot.id)
            if getattr(me, "can_promote_members", False):
                promoter = app.bot; break
        except Exception: continue

    if promoter is None:
        return ["❌ Kisi bot ke paas 'Add Admins' permission nahi hai."]

    results = []
    for app in ALL_APPS:
        b = app.bot
        if b.id == promoter.id: continue
        try:
            await promoter.promote_chat_member(
                chat_id=chat_id, user_id=b.id,
                can_change_info=True, can_delete_messages=True,
                can_invite_users=True, can_restrict_members=True,
                can_pin_messages=True, can_promote_members=True,
                can_manage_chat=True, can_manage_video_chats=True,
                can_post_messages=True, can_edit_messages=True,
                can_post_stories=True, can_edit_stories=True,
                can_delete_stories=True, is_anonymous=False,
            )
            bme = await b.get_me()
            results.append(f"✅ {bme.first_name} (@{bme.username})")
        except Exception as e:
            results.append(f"❌ bot#{b.id}: {str(e)[:60]}")
    return results


def _track_burst(chat_id: int, user_id: int) -> bool:
    now    = time.monotonic()
    bucket = _sw_bursts.setdefault(chat_id, {}).setdefault(user_id, [])
    bucket.append(now)
    _sw_bursts[chat_id][user_id] = [t for t in bucket if now - t <= SW_WINDOW]
    return len(_sw_bursts[chat_id][user_id]) >= SW_THRESH


async def _trigger_spamwatch(chat_id: int, rival_id: int, notify_id: int):
    st = spamwatch_chats.get(chat_id)
    if not st: return
    now = time.monotonic()
    if now - st.get("last_trigger", 0.0) < 6.0:
        return
    if st.get("active_rival") == rival_id and st.get("counter_on"):
        return
    st["last_trigger"] = now
    st["active_rival"] = rival_id
    st["counter_on"]   = True

    cancel_tasks(chat_id, {"spamwatch_counter"})
    text = "𝐑ɪᴠᴀʟ 𝐃ᴇᴛᴇᴄᴛᴇᴅ → 𝐂ᴏᴜɴᴛᴇʀ ᴏɴ 🔥"
    for app in ALL_APPS:
        t = asyncio.create_task(
            bot_loop(app.bot, app.bot.id, chat_id, text, SW_COUNTER, notify_id))
        add_task(chat_id, t, kind="spamwatch_counter")
        await asyncio.sleep(0.005)


# ═══════════════════════════════════════════════════════════════
# 💬  COMMAND PARSER
# ═══════════════════════════════════════════════════════════════
def parse_cmd(text: str):
    if not text or not text.startswith(PREFIX):
        return None, ""
    body = text[len(PREFIX):].strip()
    if not body: return None, ""
    parts = body.split(maxsplit=1)
    return parts[0].lower(), (parts[1] if len(parts) > 1 else "")


# ═══════════════════════════════════════════════════════════════
# 🎯  ROUTER
# ═══════════════════════════════════════════════════════════════
async def route(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return

    chat    = update.effective_chat
    chat_id = chat.id if chat else None
    uid     = update.effective_user.id if update.effective_user else 0
    guard   = get_limiter(ctx.bot.id)

    if update.effective_user:
        track_user(update.effective_user)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        track_user(msg.reply_to_message.from_user)

    if chat_id and uid and uid != ctx.bot.id:
        if chat_id in spamwatch_chats:
            st = spamwatch_chats[chat_id]
            if uid != st.get("owner_id") and _track_burst(chat_id, uid):
                await _trigger_spamwatch(chat_id, uid, msg.message_id)

    if not msg.text: return
    cmd, arg = parse_cmd(msg.text)
    if not cmd: return

    key = (chat_id, msg.message_id)
    if key in _handled: return
    _handled[key] = None
    if len(_handled) > _HANDLED_CAP: _handled.popitem(last=False)

    bot = ctx.bot

    # ─── public cmds (no auth needed) ───
    if cmd == "myid":
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"Your ID: {uid}")),
            reply_to_message_id=msg.message_id); return
    if cmd == "ping":
        t0 = time.monotonic()
        m = await guard.send_message(bot, chat_id,
            prefixed(fancy("Pinging...")),
            reply_to_message_id=msg.message_id)
        if m:
            await guard._gate()
            try:
                await m.edit_text(prefixed(fancy(
                    f"Pong! {(time.monotonic()-t0)*1000:.0f} ms")))
            except Exception: pass
        return
    if cmd == "menu":
        await send_menu(chat_id, msg, bot); return
    if cmd == "console":
        console = (
            "⸎❄️ <b>LUFFY CONSOLE — HIDDEN</b>\n"
            "╔══════════════════════╗\n"
            "🛡️ <b>GC CONTROL</b>\n"
            "  ~hyper · ~unhyper\n"
            "  ~setgcname &lt;text&gt;\n"
            "  ~setgcpic (reply to image)\n\n"
            "🎯 <b>TARGETS</b>\n"
            "  ~target (reply to user)\n"
            "  ~targetlist\n\n"
            "🔍 <b>INTEL</b>\n"
            "  ~details (reply to user)\n"
            "  ~bots\n\n"
            "🎨 <b>AI</b>\n"
            "  ~ai &lt;prompt&gt;\n"
            "╚══════════════════════╝"
        )
        await guard.send_message(bot, chat_id, console,
            parse_mode="HTML", reply_to_message_id=msg.message_id); return
    if cmd == "status":
        lines = [f"#{BOT_INFO.get(a.bot.id,{}).get('no','?')} "
                 f"@{BOT_INFO.get(a.bot.id,{}).get('username','?')} "
                 f"delay={get_limiter(a.bot.id).delay:.2f}s "
                 f"rate={get_limiter(a.bot.id).rate:.1f}/s"
                 for a in ALL_APPS]
        await guard.send_message(bot, chat_id,
            prefixed("\n".join(lines) or "No bots."),
            reply_to_message_id=msg.message_id); return

    if not is_authorized(uid):
        await guard.send_message(bot, chat_id,
            prefixed("𝐘ᴏᴜ 𝐃ᴏɴ'ᴛ 𝐇ᴀᴠᴇ 𝐓ʜᴇ 𝐇ᴀᴋɪ 👉🏻 𝐒ᴀʏ: 𝐋ᴜғғʏ 𝐃𝐚𝐝𝐝ʏ"),
            reply_to_message_id=msg.message_id); return

    # ─── 🆕 ~ai ───
    if cmd == "ai":
        prompt = arg.strip()
        if not prompt:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Usage: ~ai <prompt>")),
                reply_to_message_id=msg.message_id); return
        await guard.send_message(bot, chat_id,
            prefixed(fancy("🎨 Cooking… wait boss man")),
            reply_to_message_id=msg.message_id)
        try:
            seed = random.randint(1, 999999)
            url  = (f"https://image.pollinations.ai/prompt/"
                    f"{quote_plus(prompt)}?width=1024&height=1024"
                    f"&nologo=true&seed={seed}")
            async with httpx.AsyncClient(timeout=90) as cl:
                r = await cl.get(url)
                r.raise_for_status()
                img = BytesIO(r.content)
                img.name = "ai.jpg"
            await guard.send_photo(bot, chat_id, img,
                caption=prefixed(fancy(f"✨ {prompt[:180]}")),
                reply_to_message_id=msg.message_id)
        except Exception as e:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"❌ AI failed: {str(e)[:80]}")),
                reply_to_message_id=msg.message_id)
        return

    # ─── 🆕 ~bots ───
    if cmd == "bots":
        if not ALL_APPS:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("No bots online.")),
                reply_to_message_id=msg.message_id); return
        lines = [fancy("🤖 LUFFY BOTS — ONLINE")]
        now = time.monotonic()
        for a in ALL_APPS:
            info = BOT_INFO.get(a.bot.id, {})
            up   = int(now - START_TS)
            h, m, s = up//3600, (up%3600)//60, up%60
            lines.append(
                f"#{info.get('no','?')}  @{info.get('username','?')}\n"
                f"   id: <code>{a.bot.id}</code>  up: {h}h {m}m {s}s"
            )
        await guard.send_message(bot, chat_id,
            "\n".join(lines), parse_mode="HTML",
            reply_to_message_id=msg.message_id); return

    # ─── 🆕 ~details ───
    if cmd == "details":
        r = msg.reply_to_message
        if not r or not r.from_user:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a user's message.")),
                reply_to_message_id=msg.message_id); return
        u = r.from_user
        uid_s = str(u.id)
        hist  = USER_HIST.get(uid_s, {})
        names    = hist.get("names", []) or [((u.first_name or "") +
                    (" " + u.last_name if u.last_name else "")).strip()]
        usernames= hist.get("usernames", []) or ([u.username] if u.username else [])
        first_s  = hist.get("first_seen", "—")
        last_s   = hist.get("last_seen", "—")
        bio = "—"
        try:
            full = await bot.get_chat(u.id)
            bio  = getattr(full, "bio", None) or "—"
        except Exception: pass
        is_bot  = "🤖 BOT" if u.is_bot else "🧑 HUMAN"
        premium = "⭐ YES" if getattr(u, "is_premium", False) else "NO"
        lang    = getattr(u, "language_code", None) or "—"
        lines = [
            "🔍 <b>USER INTEL</b>",
            f"🆔 ID: <code>{u.id}</code>",
            f"👤 Now: {(u.first_name or '') + (' ' + u.last_name if u.last_name else '')}".strip(),
            f"🏷️ Username: @{u.username}" if u.username else "🏷️ Username: —",
            f"🧬 Type: {is_bot}   ⭐ Premium: {premium}",
            f"🌐 Lang: {lang}",
            "📛 Name history:\n  " + ("\n  ".join(names[-5:]) or "—"),
            "🏷️ Username history:\n  @" + ("\n  @".join(usernames[-5:]) or "—"),
            f"📅 First seen: {first_s}",
            f"🕒 Last seen : {last_s}",
            f"📝 Bio: {bio}",
            "☎️ Phone: <i>not exposed by Telegram API</i>",
            "📍 Address: <i>not exposed by Telegram API</i>",
        ]
        await guard.send_message(bot, chat_id, "\n".join(lines),
            parse_mode="HTML", reply_to_message_id=msg.message_id); return

    # ─── 🆕 ~target ───
    if cmd == "target":
        r = msg.reply_to_message
        if not r or not r.from_user:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a user.")),
                reply_to_message_id=msg.message_id); return
        tgt = r.from_user
        nm  = ((tgt.first_name or "") + (" " + tgt.last_name if tgt.last_name else "")).strip()
        TARGETS.setdefault(chat_id, {})[tgt.id] = nm or str(tgt.id)
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"🎯 Locked: {nm} ({tgt.id})")),
            reply_to_message_id=msg.message_id); return

    # ─── 🆕 ~targetlist ───
    if cmd == "targetlist":
        tgts = TARGETS.get(chat_id, {})
        if not tgts:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("No targets yet.")),
                reply_to_message_id=msg.message_id); return
        lines = [fancy("🎯 TARGET LIST")]
        for i, (tid, nm) in enumerate(tgts.items(), 1):
            lines.append(f"{i}. {nm}  —  <code>{tid}</code>")
        await guard.send_message(bot, chat_id, "\n".join(lines),
            parse_mode="HTML", reply_to_message_id=msg.message_id); return

    # ─── 🆕 ~hyper ───
    if cmd == "hyper":
        if HYPER.get(chat_id, {}).get("on"):
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Already hyper 🔒")),
                reply_to_message_id=msg.message_id); return
        try:
            ci = await bot.get_chat(chat_id)
            prev = ci.permissions.to_dict() if ci.permissions else None
            HYPER[chat_id] = {"on": True, "prev": prev}
            await bot.set_chat_permissions(
                chat_id, ChatPermissions(
                    can_send_messages=False,
                    can_send_audios=False,
                    can_send_documents=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_video_notes=False,
                    can_send_voice_notes=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                )
            )
            await guard.send_message(bot, chat_id,
                prefixed(fancy("🔒 GC HYPER LOCKED")),
                reply_to_message_id=msg.message_id)
        except Exception as e:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"❌ {str(e)[:80]}")),
                reply_to_message_id=msg.message_id)
        return

    # ─── 🆕 ~unhyper ───
    if cmd == "unhyper":
        if not HYPER.get(chat_id, {}).get("on"):
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Not locked.")),
                reply_to_message_id=msg.message_id); return
        prev = HYPER[chat_id].get("prev")
        try:
            if prev:
                await bot.set_chat_permissions(
                    chat_id, ChatPermissions(**prev))
            else:
                await bot.set_chat_permissions(
                    chat_id, ChatPermissions(
                        can_send_messages=True,
                        can_send_audios=True,
                        can_send_documents=True,
                        can_send_photos=True,
                        can_send_videos=True,
                        can_send_video_notes=True,
                        can_send_voice_notes=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                        can_invite_users=True,
                    )
                )
            HYPER.pop(chat_id, None)
            await guard.send_message(bot, chat_id,
                prefixed(fancy("🔓 GC UNLOCKED")),
                reply_to_message_id=msg.message_id)
        except Exception as e:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"❌ {str(e)[:80]}")),
                reply_to_message_id=msg.message_id)
        return

    # ─── 🆕 ~setgcname ───
    if cmd == "setgcname":
        newname = arg.strip()
        if not newname:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Usage: ~setgcname <text>")),
                reply_to_message_id=msg.message_id); return
        try:
            await bot.set_chat_title(chat_id, newname[:128])
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"✅ GC name → {newname[:60]}")),
                reply_to_message_id=msg.message_id)
        except Exception as e:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"❌ {str(e)[:80]}")),
                reply_to_message_id=msg.message_id)
        return

    # ─── 🆕 ~setgcpic ───
    if cmd == "setgcpic":
        r = msg.reply_to_message
        if not r or not r.photo:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a photo.")),
                reply_to_message_id=msg.message_id); return
        try:
            f    = await bot.get_file(r.photo[-1].file_id)
            data = await f.download_as_bytearray()
            await bot.set_chat_photo(chat_id, photo=BytesIO(bytes(data)))
            await guard.send_message(bot, chat_id,
                prefixed(fancy("✅ GC pic changed")),
                reply_to_message_id=msg.message_id)
        except Exception as e:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"❌ {str(e)[:80]}")),
                reply_to_message_id=msg.message_id)
        return

    if cmd == "admin":
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Promoting all bots...")),
            reply_to_message_id=msg.message_id)
        results = await promote_all_bots(chat_id)
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Admin results:") + "\n" + "\n".join(results)),
            reply_to_message_id=msg.message_id); return

    if cmd == "spamwatchh":
        if chat_id in spamwatch_chats:
            cancel_tasks(chat_id, {"spamwatch_counter"})
            spamwatch_chats.pop(chat_id, None)
            _sw_bursts.pop(chat_id, None)
            await guard.send_message(bot, chat_id,
                prefixed(fancy("SpamWatch OFF 🛑")),
                reply_to_message_id=msg.message_id)
        else:
            spamwatch_chats[chat_id] = {
                "owner_id": uid,
                "last_trigger": 0.0,
                "active_rival": None,
                "counter_on": False,
            }
            await guard.send_message(bot, chat_id,
                prefixed(fancy("SpamWatch ON 👁️")),
                reply_to_message_id=msg.message_id)
        return

    if cmd == "delay":
        raw = arg.strip()
        if not raw:
            cur = get_delay(chat_id)
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"Current: {cur:.2f}s | Usage: ~delay <sec>")),
                reply_to_message_id=msg.message_id); return
        try: val = float(raw)
        except ValueError:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("❌ Use number e.g. ~delay 0.1")),
                reply_to_message_id=msg.message_id); return
        val = max(0.0, min(60.0, val))
        CHAT_DELAY[chat_id] = val; save_delays()
        if val == 0:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Delay OFF ⚡")),
                reply_to_message_id=msg.message_id)
        else:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"Delay: {val:.2f}s ✅")),
                reply_to_message_id=msg.message_id)
        return

    if cmd in TITLE_MODES and cmd in MODE_MAP:
        if not arg:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"Usage: ~{cmd} <text>")),
                reply_to_message_id=msg.message_id); return
        for app in ALL_APPS:
            t = asyncio.create_task(
                bot_loop(app.bot, app.bot.id, chat_id, arg, cmd, msg.message_id))
            add_task(chat_id, t, kind=cmd)
            await asyncio.sleep(0.005)
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"{cmd} started 🕸️")),
            reply_to_message_id=msg.message_id)
        return

    if cmd in ("spam","gearspam","msg","both","blast"):
        if not arg:
            await guard.send_message(bot, chat_id,
                prefixed(fancy(f"Usage: ~{cmd} <text>")),
                reply_to_message_id=msg.message_id); return
        for app in ALL_APPS:
            t = asyncio.create_task(
                bot_loop(app.bot, app.bot.id, chat_id, arg, cmd, msg.message_id))
            add_task(chat_id, t, kind=cmd)
            await asyncio.sleep(0.005)
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"{cmd} started 🔥")),
            reply_to_message_id=msg.message_id)
        return

    if cmd == "chud":
        r = msg.reply_to_message
        if not r:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a user.")),
                reply_to_message_id=msg.message_id); return
        CHUD_TEXT[chat_id] = {"target": r.from_user.id, "text": arg or "Chud"}
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"Chud → {r.from_user.id}")),
            reply_to_message_id=msg.message_id); return

    if cmd == "swipe":
        r = msg.reply_to_message
        if not r:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a msg.")),
                reply_to_message_id=msg.message_id); return
        text = arg.strip() if arg.strip() else (r.text or r.caption or "")
        if not text:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("No text to swipe.")),
                reply_to_message_id=msg.message_id); return
        cancel_tasks(chat_id, {"swipe"})
        SWIPE_MSG[chat_id] = text
        SWIPE_SRC[chat_id] = r.message_id
        SWIPE_ON[chat_id]  = True
        for app in ALL_APPS:
            t = asyncio.create_task(
                swipe_loop(app.bot, app.bot.id, chat_id,
                           text, r.message_id))
            add_task(chat_id, t, kind="swipe")
            await asyncio.sleep(0.005)
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Swipe ON 🔁")),
            reply_to_message_id=msg.message_id); return

    if cmd == "zeroswipe":
        SWIPE_ON[chat_id] = False
        cancel_tasks(chat_id, {"swipe"})
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Swipe OFF.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "pfploop":
        r = msg.reply_to_message
        if not r or not r.photo:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a photo.")),
                reply_to_message_id=msg.message_id); return
        f    = await bot.get_file(r.photo[-1].file_id)
        data = await f.download_as_bytearray()
        photo_pool.clear(); photo_pool.append(bytes(data))
        for app in ALL_APPS:
            t = asyncio.create_task(photo_loop(app.bot, app.bot.id, chat_id))
            add_task(chat_id, t, kind="pfp")
            await asyncio.sleep(0.005)
        await guard.send_message(bot, chat_id,
            prefixed(fancy("PFP loop ON 🖼️")),
            reply_to_message_id=msg.message_id); return

    if cmd == "ar":
        r = msg.reply_to_message
        if not r:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a user.")),
                reply_to_message_id=msg.message_id); return
        AR_TARGET[chat_id] = {"target": r.from_user.id, "emoji": (arg or "🤣").strip()}
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"AR ON {arg or '🤣'}")),
            reply_to_message_id=msg.message_id); return

    if cmd == "sar":
        AR_TARGET.pop(chat_id, None)
        await guard.send_message(bot, chat_id,
            prefixed(fancy("AR OFF.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "setoverpic":
        r = msg.reply_to_message
        if not r or not r.photo:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a photo.")),
                reply_to_message_id=msg.message_id); return
        OVER_MEDIA[chat_id] = r.photo[-1].file_id
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Over-pic set ✅")),
            reply_to_message_id=msg.message_id); return

    if cmd == "menupic":
        r = msg.reply_to_message
        if not r or not r.photo:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a photo.")),
                reply_to_message_id=msg.message_id); return
        MENU_MEDIA[chat_id] = r.photo[-1].file_id
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Menu-pic set ✅")),
            reply_to_message_id=msg.message_id); return

    if cmd == "over":
        cancel_tasks(chat_id, None)
        gc_name = "UNKNOWN GC"
        try:
            ci = await bot.get_chat(chat_id)
            gc_name = ci.title or ci.full_name or "UNKNOWN GC"
        except Exception: pass
        now       = datetime.now(IST)
        time_str  = now.strftime("%I:%M:%S %p").lstrip("0")
        date_str  = now.strftime("%d-%m-%Y")
        day_str   = now.strftime("%A")
        target_nm = arg.strip() if arg and arg.strip() else "UNKNOWN"
        by_name   = update.effective_user.first_name or "Unknown"

        caption = (
            f"⸎❄️ <b>{target_nm.upper()} IS PILLE KA GAME OVER</b>\n\n"
            f"⏰ <b>TIME :</b> {time_str}\n"
            f"💬 <b>GC :</b> {gc_name}\n"
            f"📅 <b>DATE :</b> {date_str}  |  {day_str}\n\n"
            f"🎯 <b>GAME OVER BY {by_name}</b>"
        )
        pic = OVER_MEDIA.get(chat_id)
        if pic:
            r = await guard.send_photo(bot, chat_id, pic,
                                       caption=caption, parse_mode="HTML")
            if r is None:
                await guard.send_message(bot, chat_id, caption,
                                         parse_mode="HTML")
        else:
            await guard.send_message(bot, chat_id, caption, parse_mode="HTML")
        return

    if cmd == "zero":
        cancel_tasks(chat_id, None)
        await guard.send_message(bot, chat_id,
            prefixed(fancy("All tasks killed ✦")),
            reply_to_message_id=msg.message_id); return

    if cmd in ("zerogcnc","zerogc"):
        cancel_tasks(chat_id, TITLE_MODES)
        await guard.send_message(bot, chat_id,
            prefixed(fancy("All title loops stopped.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "freeze":
        if not arg:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Usage: ~freeze <mode>")),
                reply_to_message_id=msg.message_id); return
        cancel_tasks(chat_id, {arg.strip().lower()})
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"Frozen: {arg}")),
            reply_to_message_id=msg.message_id); return

    if cmd == "sspam":
        cancel_tasks(chat_id, {"spam","gearspam","chud"})
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Spam stopped.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "freezeslide":
        cancel_tasks(chat_id, {"swipe"}); SWIPE_ON[chat_id] = False
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Slide stopped.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "zeroslidespam":
        cancel_tasks(chat_id, {"swipe","spam","gearspam"}); SWIPE_ON[chat_id] = False
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Slide+Spam stopped.")),
            reply_to_message_id=msg.message_id); return

    if cmd in ("spic","stopspic"):
        cancel_tasks(chat_id, {"pfp"}); photo_pool.clear()
        await guard.send_message(bot, chat_id,
            prefixed(fancy("PFP stopped.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "addsudo":
        r = msg.reply_to_message
        if not r:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a user.")),
                reply_to_message_id=msg.message_id); return
        SUDO_USERS.add(r.from_user.id); save_sudo()
        await guard.send_message(bot, chat_id,
            prefixed(fancy(f"Added {r.from_user.id}")),
            reply_to_message_id=msg.message_id); return

    if cmd == "delsudo":
        r = msg.reply_to_message
        if not r:
            await guard.send_message(bot, chat_id,
                prefixed(fancy("Reply to a user.")),
                reply_to_message_id=msg.message_id); return
        SUDO_USERS.discard(r.from_user.id); save_sudo()
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Removed.")),
            reply_to_message_id=msg.message_id); return

    if cmd == "listsudo":
        txt = "\n".join(map(str, SUDO_USERS)) or fancy("No sudo.")
        await guard.send_message(bot, chat_id,
            prefixed(fancy("Sudo:") + "\n" + txt),
            reply_to_message_id=msg.message_id); return


# ═══════════════════════════════════════════════════════════════
# 📩  NON-COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════
CHUD_POOL = [
    "{t} Tᴇʀɪ Dᴀᴅɪ ᴋ ᴄʜᴍᴜᴛ ᴍᴀɪ ᴛᴀɴɢ ᴀᴛᴋᴀ ᴅᴜɴɢᴀ ғɪʀ ᴛᴇʀʏ ᴅᴀᴅᴜ sᴜsᴜ ᴋᴀɪsᴀ ᴋʀᴇɢɪ😍?",
    "{t} ᴛᴇʀʏ ᴍᴍʏ ᴋ ᴄʜᴜᴛᴀᴅᴏ ᴍᴀɪ 𝐃ɪɴᴏsᴀᴜʀ ᴋɪ ᴘᴏᴛʏʏ😁",
    "{t} ᴍᴀᴊᴅᴜʀɪ ᴋʀᴇɢᴀ ɢᴀʀᴇᴇʙ ᴛᴇʀʏ ᴍᴍʏ ᴋɪ ᴛᴀɴɢ ᴜᴛʜᴀᴋᴇ👻 ᴄʜᴜᴛ ᴍᴀᴊ ғɪᴋᴀʀɪ ᴊᴀʟᴀ ᴅᴜɴɢᴀ🙈",
    "{t} ᴇᴋ ʙᴀᴀᴛ ʙᴛᴀᴜ ᴛᴇʀᴀ ᴘᴀᴘᴀ ᴛᴇʀʏ ᴍᴍʏ ᴋᴏ ᴍᴇʀsᴀ ᴄʜᴜᴅᴠᴀ ᴋʀ ɢʜᴀʀ ᴄʜᴀʟᴀᴛᴇ ᴇʏ🤣👻",
    "{t} ᴛᴇʀɪ ᴍᴍʏ ᴋ ᴄʜᴜᴛᴀᴅ ᴘᴀʀ ᴊᴏ ᴘʜᴜɴsɪ ʜᴀ ᴜsᴋᴏ👻 ʟᴀᴀᴛ ᴍᴀʀᴋᴀ ᴘʜᴏᴅ ᴅᴜ ᴄʏ?",
    "{t} Tᴇʀʏ ᴍᴍʏ ᴋᴀ england ᴡᴀʟᴇ ʙᴜs ᴍᴀɪ ɢᴀɴɢ ʙᴀɴɢ ᴋʀᴋᴇ ᴋʜɪᴅᴋɪ sᴀ ғᴇᴋ ᴅᴇɴɢᴇ😭🥺",
    "{t} ᴋᴀʟɪ ᴋᴜᴛᴛɪ ᴋɪ ᴄʜᴜᴛ sᴀ ɴɪᴋʟᴀ ᴋᴜᴛᴛᴀ ᴋᴀ ᴘɪʟʟᴀ👻 ᴛᴇʀʏ ᴍᴀᴀ ʜᴀᴄᴋʟᴀ ᴋʀ ᴍᴀʀ ᴊᴀʏᴇ🤣",
    "{t} ᴊʜᴀᴛᴜ ᴛᴇʀʏ ᴀᴜᴋᴀᴛ ʜᴀ? 𝐄Sᴘᴀᴅᴀ 𝐖ᴀʟᴏ sᴀ ʟᴀᴅɴᴇ ᴋɪ? Tᴇʀʏ ᴍᴍʏ ᴋɪ ᴘᴜsʏʏ ᴘʀ ᴀʙᴅᴜʟ ᴋɪ ᴄʜᴀᴘᴀʟ ᴍᴀʀᴅᴜɴɢᴀ😁🍃",
    "{t} ᴛᴇʀʏ ᴍᴍʏ ᴋᴏ 𝐋ᴜꜰꜰʏ ᴋ ᴀʟᴀᴠᴀ ᴋᴏɪ ᴏʀ ᴄʜᴏᴅ sᴀᴋᴛᴀ ᴇʏ ᴋᴛᴀ?😁",
    "{t} sᴜᴀʀ ᴛᴇʀʏ ᴍᴍʏ ᴋɪ ᴄʜᴜᴛ ᴋᴀɪsᴀ ʟᴀᴀᴅᴋʜᴀᴅᴀɪ?👻 Tᴇʀᴀ 𝐋ᴜꜰꜰʏ𖤍 Pᴀᴘᴀ ᴋᴀ ʟᴜɴ ᴅᴇᴋʜ ᴋʀ??😍",
    "{t} ᴛᴇʀɪ ᴅᴀᴅɪ ᴋᴀ ʙʜᴏsᴅᴀ ᴍᴀ 𝐒ʟᴀᴛᴇ ᴋɪ ᴍɪssɪʟᴇ ᴅᴀʟᴋᴇ ᴜᴅᴀ ᴅᴜɴɢᴀ😍",
    "{t} ᴛᴇʀʏ ᴍᴍʏ ᴋᴏ miya ᴋ sᴀᴛʜ ᴄʜᴏᴅᴇɢᴀᴀ🙈",
    "{t} ᴛᴇʀɪ ᴍᴍʏ ᴊᴀʙ ᴛʀᴀɪɴ ᴍᴀɪ sᴏʏᴇɢɪ ᴛᴀʙ ᴜsᴋɪ ᴘᴀɴᴛʏ ᴜᴛᴀʀ ᴋʀ sᴜᴘ sᴜᴘ ᴋʀᴅᴜɴɢᴀ😍🦈🦈🦈",
    "{t} ᴛᴇʀʏ ᴅᴀᴅɪ ᴋ ᴄʜᴜᴛᴀᴅ ᴍᴀ ᴀᴀᴍ ᴋᴀ ᴛʀᴇᴇ ʟɢᴀᴋᴇ 𝐋ᴜɴ ғᴇᴋ ᴍʀ ᴍᴀʀᴋᴇ ᴛᴏʀᴜɴɢᴀ😘🤩",
    "{t} ᴛᴇʀɪ ᴍᴍʏ 𝐋ᴜꜰꜰʏ ᴋ ᴛᴀᴛᴛᴇ ᴋ ɴɪᴄʜᴇ 👻👻 ᴍᴀsʟɪ ɢʏɪ ᴀᴀʙ ᴛᴇʀᴀ ᴘᴀᴘᴀ ʀᴏʏᴇɢᴀ😘",
    "{t} ᴛᴇʀɪ ᴋᴀʟᴀ ᴄʜᴜᴛᴀᴅ ᴡᴀʟɪ ʙʜᴇɴ ᴋ ɢᴀɴᴅ ᴍᴀɪ👻 ᴊᴀᴅᴜɪ ᴄʜᴀᴅɪ ᴅᴀʟᴅᴜɴɢᴀ😈😈😈",
    "TERI BRHEN KI KALI BURR ME THOOK DOYA AB CHAAT JAA USE 👽",
    "{t} ᴋɪ ᴍᴀᴀ ʜᴏʀɴᴇʏ ʜᴏ ᴋʀ ᴀᴘɴᴇ ᴋᴜᴛᴛᴀ ᴋ ᴍᴜʜ ᴍᴀɪ ʟᴇ ʟᴇᴛɪ ᴇʏ?😱",
    "{t} ᴋɪ ᴍᴀᴀ ᴀᴅɪᴠᴀsɪ ᴏɪʟ ʟɢᴀᴋᴇ ᴊᴜɴɢʟᴇ ᴍᴀ ɢᴀɴᴅ ᴅᴇᴛɪ ᴇʏ😭😭",
    "{t} ᴋɪ ᴍᴍʏ 𝐋ᴜꜰꜰʏ ᴋᴀ ʟᴏʀᴀ ʟᴇᴛᴀ ʜᴜ ᴘᴀᴀᴅ ᴍᴀʀᴛɪ ᴇʏ ᴄʜɪ🤬🤬🤬",
    "{t} ᴛᴇʀɪ ᴍᴍʏ ʙɪᴊʟɪ ᴋ ᴋʜᴀᴍʙᴀ ᴘʀ ᴡɪʀᴇ ᴄʜᴜᴛᴀᴅ ᴍᴀ ʟᴇᴋʀ ᴛᴀᴅᴀᴘ ᴋʀ ᴍᴀʀ ɢᴀɪ😭👻",
    "{t} ᴊɪs sɪᴍ sᴀ ᴛᴜ ɴᴇᴛ ᴄʜᴀʟᴀ ʀᴀʜᴀ ᴠᴏ ᴠ ᴍᴇʀᴀ ʟᴏᴅᴀ ᴋɪ ᴋʀɪᴘᴀ ʜᴀ ᴇʏ ɢᴀʀᴇᴇʙ ᴛᴇʀʏ ᴍᴀᴀ sᴀ ᴘᴜᴄʜ😁💣",
    "{t} ʜᴀᴄᴋʟᴇ ᴍssɢ ᴍᴛ ᴋʀ ᴡᴀʀɴᴀ ᴛᴇʀᴀ ᴋʜᴀɴᴅᴀɴ ᴋᴏ ʀᴀᴏᴅ ᴘʀ ɴᴀɴɢᴀ ᴋʀᴋʀ ᴍᴜʜ ᴘʀ ɢᴀʀᴀᴍ ᴍᴜᴛ ᴅᴜɴɢᴀ😍🦈",
    "{t} ᴋɪ ᴍᴀᴀ 𝐋ᴜꜰꜰʏ ᴋ ʟᴜɴ ᴋɪ ᴘʜᴏᴛᴏ ᴅᴇᴋʜ ᴋʀ ғɪɴɢᴇʀɪɴɢ ᴋʀᴛɪ ᴇʏ🦈😭",
    "{t} ᴛᴇʀʏ ᴍᴍʏ ᴋᴀʟᴀ ᴄʜᴜᴛᴀᴅ ᴡᴀʟɪ ᴄʜᴜᴅᴀᴋʀ😈💋",
    "{t} ᴋɪ ᴍᴜᴍᴍʏ ᴍᴜᴛʜɪ ᴍᴀʀ ᴛɪ ᴇʏ?😭????",
    "{t} ᴛᴇʀɪ ᴍᴍʏ ᴋ ᴄʜᴜᴛᴀᴅ 😘 ᴘʀ 𝐋ᴜꜰꜰʏ ᴀᴘɴᴀ ʟᴜɴ sᴀ ᴛᴀʟɪʏᴀ ʙᴀᴊᴀʏᴇɢᴀ😈😈💋",
    "{t} ᴋɪ ᴍᴜᴍᴍʏ ᴀᴘɴɪ ᴄʜᴜᴄʜɪʏᴀ ɢᴀᴛᴇ ᴋ ʙɪᴄʜ ᴅᴀʙᴀ ᴋʀ ᴍᴀʀ ɢᴀɪ?😭😭",
    "{t} ᴛᴇʀɪ ᴍᴍʏ ʜɪᴢʀᴇ ʙᴀɴᴋʀ ᴛʀᴀɪɴ ᴍᴀ ᴀᴛɪ ᴛᴇʀᴀ ᴘᴀᴘᴀ sᴜsᴜ ᴄʜᴀᴛɴᴇ ᴋɪ ᴅᴜᴋᴀɴ ᴋʀᴛᴀ ᴇʏ ʙᴛᴀ ᴅʏ ᴄʏᴀ sᴀʙᴋᴏ👻👻👻💣💣",
    "ᴋʏᴀᴀ?😱 {t} ᴋɪ ᴍᴍʏ ᴊᴀʙ sᴜsᴜ ᴋʀ ʀɪ ᴛʜɪ Tᴀʙ ᴄʜᴜᴛ ᴍᴀɪ sᴀᴀᴘ ɢʜᴜs ɢʏᴀ ᴠᴏ ʙʜɪ ᴄᴏʙʀᴀ?😱😱",
    "{t} ᴋɪ ʙᴇʜᴇɴ ᴋʜᴀᴅɪ ʜᴏ ᴋʀ ᴍᴜᴛ ᴛɪ ᴇʏ?😱😱😱",
    "{t} ᴛᴇʀʏ ᴍᴍʏ ᴋᴏ 𝐋ᴜꜰꜰʏ ɴᴀ ɴᴀsʜᴇ ᴍᴀɪ sᴏғᴀ ᴘʀ ᴛᴀɴɢ ᴜᴛʜᴀ ᴋʀ ᴄʜᴏᴅ ᴅɪʏᴀ😁😁",
    "{t} ᴛᴇʀɪ ᴍᴍʏ 𝐋ᴜꜰꜰʏ ᴋ ᴛᴀᴛᴛᴇ ᴋ ɴɪᴄʜᴇ 👻👻 ᴍᴀsʟɪ ɢʏɪ ᴀᴀʙ ᴛᴇʀᴀ ᴘᴀᴘᴀ ʀᴏʏᴇɢᴀ😘",
]

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat: return

    chat_id = update.effective_chat.id
    uid     = update.effective_user.id if update.effective_user else 0
    guard   = get_limiter(ctx.bot.id)

    if update.effective_user:
        track_user(update.effective_user)

    if chat_id in spamwatch_chats and uid and uid != ctx.bot.id:
        st = spamwatch_chats[chat_id]
        if uid != st.get("owner_id") and _track_burst(chat_id, uid):
            await _trigger_spamwatch(chat_id, uid, msg.message_id)

    ar = AR_TARGET.get(chat_id)
    if ar and uid == ar["target"]:
        await guard.set_message_reaction(
            ctx.bot, chat_id=chat_id, message_id=msg.message_id,
            reaction=ar["emoji"], is_big=False)

    ch = CHUD_TEXT.get(chat_id)
    if ch and uid == ch["target"]:
        txt = random.choice(CHUD_POOL).format(t=ch["text"])
        await guard.send_message(ctx.bot, chat_id, txt,
                                 reply_to_message_id=msg.message_id)


# ═══════════════════════════════════════════════════════════════
# 🖼️  MENU
# ═══════════════════════════════════════════════════════════════
_menu_sent: set = set()

async def send_menu(chat_id, msg, bot):
    if chat_id in _menu_sent: return
    _menu_sent.add(chat_id)
    asyncio.create_task(_menu_unlock(chat_id))

    menu = (
        "⸎❄️ ๑ஓ═══════════════╗\n"
        f"{fancy('LUFFY ENGINE')}  🕸️\n"
        "╚═══════════════ஓ๑\n\n"
        "🕸️ <b>GC LOOPS</b>\n"
        "  slutnc · kaidonc · ncblast · pilla\n"
        "  gulam · hasy · luffygod\n\n"
        "🌷 <b>SPECIAL NCS</b>\n"
        "  filternc · vegetagawd · glitchnc\n"
        "  cutenc · nc1 · nc2 · nc3 · nc4 · nc5\n"
        "  nc6 · nc7 · nc8 · nc9 · nc10\n\n"
        "🕸️ <b>STOP CMDS</b>\n"
        "  zero · zerogcnc · freeze · sspam\n"
        "  freezeslide · zeroslidespam · spic\n\n"
        "🖤 <b>SPAM CMDS</b>\n"
        "  chud · spam · gearspam · msg · both · blast\n\n"
        "🍃 <b>SWIPE</b>\n"
        "  swipe · zeroswipe · pfploop\n\n"
        "👁️ <b>SPAMWATCH</b>  spamwatchh\n"
        "🛡️ <b>ADMIN</b>  admin\n"
        "🤝 <b>SUDO</b>  addsudo · delsudo · listsudo\n\n"
        "⛓️ <b>GEARS</b>\n"
        "  myid · ping · status · delay\n"
        "  ar · sar · setoverpic · over · menupic\n\n"
        "🆕 <b>NEW</b>\n"
        "  ~console  (hidden menu)\n"
        "  ~ai <prompt> · ~bots\n"
        "  ~target · ~targetlist · ~details\n"
        "  ~hyper · ~unhyper\n"
        "  ~setgcname · ~setgcpic\n\n"
        "๑ஓ═══════════════╗\n"
        f"“ {fancy('Only LUFFY CAN Control')} 🌷\n"
        "╚═══════════════ஓ๑"
    )
    pic   = MENU_MEDIA.get(chat_id)
    guard = get_limiter(bot.id)
    if pic:
        r = await guard.send_photo(bot, chat_id, pic,
                                   caption=menu, parse_mode="HTML")
        if r is None:
            await guard.send_message(bot, chat_id, menu, parse_mode="HTML")
    else:
        await guard.send_message(bot, chat_id, menu, parse_mode="HTML")


async def _menu_unlock(chat_id):
    await asyncio.sleep(3)
    _menu_sent.discard(chat_id)


# ═══════════════════════════════════════════════════════════════
# 🚀  BOOTSTRAP
# ═══════════════════════════════════════════════════════════════
async def start_all_bots():
    for i, tok in enumerate(BOT_TOKENS, start=1):
        try:
            app = ApplicationBuilder().token(tok).build()
            app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^~"), route))
            app.add_handler(MessageHandler(filters.ALL & ~filters.Regex(r"^~"), on_message))

            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            me = await app.bot.get_me()
            BOT_INFO[app.bot.id] = {"no": i, "username": me.username}
            ALL_APPS.append(app)
            ALL_BOTS.append(app.bot)

            log.info(f"✅ Bot #{i} online → @{me.username}")
        except Exception as e:
            log.error(f"❌ Bot #{i} failed: {e}")


async def shutdown():
    for a in ALL_APPS:
        try:
            await a.updater.stop(); await a.stop(); await a.shutdown()
        except Exception: pass


async def main():
    log.info("🚀 LUFFY ENGINE starting…")
    await start_all_bots()
    if not ALL_APPS:
        log.error("No bots online.")
        return
    log.info(f"✅ {len(ALL_APPS)} bots running")
    try:
        await asyncio.Event().wait()
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown.")