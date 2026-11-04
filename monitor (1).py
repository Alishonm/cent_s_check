#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CISIA CEnT-S calendar monitor -> Telegram notifications.

Monitors ONLY this page:
    https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese

Usage:
    python monitor.py                 # run forever (check every 15 minutes)
    python monitor.py --once          # one check, then exit
    python monitor.py --test-telegram # send a test message, then exit

Exit codes: 0 = stopped normally, 1 = unexpected crash,
            2 = configuration problem, 3 = another copy is already running.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    print("Required packages are missing. Run run_windows.bat (it installs them automatically).")
    sys.exit(2)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
CALENDAR_URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_PATH = Path(os.environ.get("CENTS_STATE_PATH") or (BASE_DIR / "state.json"))
LOG_PATH = BASE_DIR / "monitor.log"
LOCK_PATH = BASE_DIR / "monitor.lock"

USER_AGENT = "CEnT-S-Calendar-Monitor/1.0 (personal notification bot; 1 request per 15 min)"
FIELD_KEYS = ["format", "university", "region", "city", "deadline", "seats", "state", "date"]
TRACKED_FIELDS = ["deadline", "seats", "state", "region"]  # may change without making a "new" session
FIELD_LABELS = {  # label used in "Previous X / New X"
    "deadline": "deadline", "seats": "seats", "state": "status", "region": "region",
}
EMPTY_CONFIRM = 3          # suspicious (empty / halved) page must repeat this many checks in a row
MAX_INDIVIDUAL_NEW = 10    # more new sessions than this are sent as one compact list
OUTBOX_LIMIT = 200
TELEGRAM_MAX = 4000

log = logging.getLogger("cent")


class ParseError(Exception):
    """The page did not contain a recognisable calendar table."""


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def norm(text: str) -> str:
    return clean(text).casefold()


def redact(text: str, token: str = "") -> str:
    """Never let the bot token appear in logs or error messages."""
    if token:
        text = text.replace(token, "***")
    return re.sub(r"bot\d+:[A-Za-z0-9_\-]+", "bot***", text)


def parse_date(text: str):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------
def _map_header(text: str):
    t = norm(text)
    if "deadline" in t or "booking" in t:
        return "deadline"
    if "universit" in t:
        return "university"
    if "region" in t or "country" in t:
        return "region"
    if "city" in t:
        return "city"
    if "seat" in t:
        return "seats"
    if "state" in t or "status" in t:
        return "state"
    if "format" in t:
        return "format"
    if "date" in t:
        return "date"
    return None


def parse_calendar(html) -> list[dict]:
    """Return a list of exam sessions found in the calendar table.

    The table is located by its column titles (not by CSS classes), so small
    layout changes on the website do not break it. Raises ParseError when no
    calendar table is found: an unrecognised page is never treated as "empty".
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        cols = None
        header_idx = 0
        for i, tr in enumerate(rows[:3]):
            mapped = [_map_header(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
            if {"university", "date", "deadline"}.issubset(set(mapped)):
                cols, header_idx = mapped, i
                break
        if cols is None:
            continue
        records, skipped = [], 0
        for tr in rows[header_idx + 1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) != len(cols):
                if len(cells) >= 3:
                    skipped += 1
                continue
            rec = {k: "" for k in FIELD_KEYS}
            for key, cell in zip(cols, cells):
                if key and not rec[key]:
                    rec[key] = clean(cell.get_text(" "))
            if rec["university"] and rec["date"]:
                records.append(rec)
        if not records and skipped:
            raise ParseError("Calendar rows did not match the table header (page layout changed?)")
        return records
    raise ParseError("No calendar table found on the page (layout changed, blocked, or error page)")


def session_id(rec: dict) -> str:
    """Stable ID: format + university + city + exam date. NOT deadline/seats/status."""
    raw = "|".join(norm(rec.get(k, "")) for k in ("format", "university", "city", "date"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def index_records(records: list[dict]) -> dict:
    out: dict = {}
    for rec in records:
        sid = base = session_id(rec)
        n = 2
        while sid in out:  # two identical rows: keep both
            sid = f"{base}-{n}"
            n += 1
        out[sid] = dict(rec)
    return out


# --------------------------------------------------------------------------
# Status helpers
# --------------------------------------------------------------------------
_UNAVAILABLE = re.compile(
    r"\b(not|no longer|no|full|closed|sold out|exhausted|expired|unavailable|finished|chiuso|esaurit\w*)\b", re.I)


def status_kind(state_text: str) -> str:
    s = clean(state_text)
    if not s or set(s) <= set("-–— "):
        return "unknown"
    return "unavailable" if _UNAVAILABLE.search(s) else "available"


# --------------------------------------------------------------------------
# Message building (Persian headers, data fields as on the website)
# --------------------------------------------------------------------------
def fmt_details(r: dict) -> str:
    labels = [("format", "Format"), ("university", "University"), ("region", "Region"),
              ("city", "City"), ("date", "Exam Date"), ("deadline", "Booking Deadline"),
              ("seats", "Available Seats"), ("state", "Status")]
    return "\n".join(f"{lab}: {r[k]}" for k, lab in labels if r.get(k))


def url_footer() -> str:
    return f"View official calendar:\n{CALENDAR_URL}"


def msg_new(r: dict) -> str:
    extra = ""
    if status_kind(r.get("state", "")) == "available":
        extra = "\n✅ ثبت‌نام در حال حاضر باز به نظر می‌رسد."
    return f"🆕 تاریخ آزمون جدید CEnT-S پیدا شد!\n\n{fmt_details(r)}{extra}\n\n{url_footer()}"


def msg_changed(old: dict, new: dict, changes: dict) -> str:
    reopened = ("state" in changes
                and status_kind(old.get("state", "")) == "unavailable"
                and status_kind(new.get("state", "")) == "available")
    if reopened:
        header = "🟢 ثبت‌نام برای این آزمون باز شد!"
    elif "deadline" in changes:
        header = "⚠️ مهلت ثبت‌نام تغییر کرد!"
    elif "seats" in changes:
        header = "🔔 تعداد صندلی‌ها (Seats) تغییر کرد!"
    elif "state" in changes:
        header = "🔔 وضعیت ثبت‌نام تغییر کرد!"
    else:
        header = "ℹ️ اطلاعات این آزمون تغییر کرد!"
    lines = [header, "", f"University: {new['university']}"]
    if new.get("format"):
        lines.append(f"Format: {new['format']}")
    if new.get("city"):
        lines.append(f"City: {new['city']}")
    lines.append(f"Exam Date: {new['date']}")
    lines.append("")
    for key in TRACKED_FIELDS:
        if key in changes:
            lab = FIELD_LABELS[key]
            o, n = changes[key]
            lines.append(f"Previous {lab}: {o or '-'}")
            lines.append(f"New {lab}: {n or '-'}")
    if reopened:
        for key, lab in (("deadline", "Booking Deadline"), ("seats", "Available Seats")):
            if key not in changes and new.get(key):
                lines.append(f"{lab}: {new[key]}")
    lines += ["", url_footer()]
    return "\n".join(lines)


def msg_removed(r: dict) -> str:
    return f"🗑 این آزمون از تقویم حذف شد:\n\n{fmt_details(r)}\n\n{url_footer()}"


def one_line(r: dict) -> str:
    bits = [r.get("university", ""), r.get("city", ""), r.get("date", "")]
    extra = []
    if r.get("deadline"):
        extra.append(f"deadline {r['deadline']}")
    if r.get("seats"):
        extra.append(f"seats {r['seats']}")
    if r.get("state"):
        extra.append(r["state"])
    line = "• " + " | ".join(b for b in bits if b)
    return line + (f" ({', '.join(extra)})" if extra else "")


def chunk_lines(header: str, lines: list[str], footer: str, limit: int = 3500) -> list[str]:
    parts, cur = [], header
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit and cur != header:
            parts.append(cur)
            cur = header
        cur += "\n" + ln
    parts.append(cur + "\n\n" + footer)
    return parts


def msg_new_compact(recs: list[dict]) -> list[str]:
    header = f"🆕 {len(recs)} تاریخ آزمون جدید CEnT-S پیدا شد!\n"
    return chunk_lines(header, [one_line(r) for r in recs], url_footer())


def msg_heartbeat(cfg, n: int) -> str:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(cfg.timezone))
    except Exception:
        now = datetime.now().astimezone()
        cfg = Config(token="", chat_id="", timezone="local")
    return f"✅ بررسی انجام شد؛ تغییری نبود.\n🕒 {now:%H:%M} ({cfg.timezone})  |  آزمون‌های ثبت‌شده: {n}"


def msg_startup(cfg, sessions: dict, baseline_created: bool, fetch_failed: bool) -> list[str]:
    head = "🤖 ربات مانیتور CEnT-S شروع به کار کرد."
    if fetch_failed:
        return [f"{head}\nهنوز نتوانستم تقویم را بخوانم؛ تلاش‌ها ادامه دارد.\n\n{url_footer()}"]
    n = len(sessions)
    every = f"بررسی هر {cfg.interval_minutes} دقیقه انجام می‌شود."
    if baseline_created:
        top = (f"{head}\nاین اولین اجراست: وضعیت فعلی تقویم به‌عنوان مبنا ذخیره شد و "
               f"فقط موارد جدید و تغییرات اعلام می‌شود.\nتعداد آزمون‌های فعلی: {n}\n{every}\n")
        if not sessions:
            return [top + "\n(در حال حاضر هیچ آزمونی در تقویم نیست.)\n\n" + url_footer()]
        recs = sorted(sessions.values(), key=lambda r: (parse_date(r["date"]) or date.max, r["university"]))
        return chunk_lines(top + "\nآزمون‌های فعلی:", [one_line(r) for r in recs[:30]], url_footer())
    return [f"{head}\nاطلاعات قبلی بازیابی شد. تعداد آزمون‌های ذخیره‌شده: {n}\n{every}\n\n{url_footer()}"]


# --------------------------------------------------------------------------
# Change detection (pure logic, easy to test)
# --------------------------------------------------------------------------
def default_state() -> dict:
    return {
        "version": 1, "initialized": False, "url": CALENDAR_URL, "sessions": {},
        "last_success": None, "outbox": [], "next_id": 1, "pending_empty": 0,
        "consecutive_failures": 0, "last_error_notice": None, "error_active": False,
    }


def apply_check(state: dict, records: list[dict], today: date):
    """Compare new records with the stored ones and update state['sessions'].

    Returns (status, messages) where status is 'baseline', 'suspicious' or 'ok'.
    """
    new = index_records(records)
    old = state["sessions"]
    if not state["initialized"]:
        state.update(sessions=new, initialized=True, pending_empty=0)
        return "baseline", []

    suspicious = (not new and bool(old)) or (len(old) >= 6 and len(new) < len(old) / 2)
    if suspicious:
        state["pending_empty"] = state.get("pending_empty", 0) + 1
        if state["pending_empty"] < EMPTY_CONFIRM:
            return "suspicious", []
    state["pending_empty"] = 0

    msgs: list[str] = []
    added = [new[s] for s in new if s not in old]
    if len(added) > MAX_INDIVIDUAL_NEW:
        msgs += msg_new_compact(added)
    else:
        msgs += [msg_new(r) for r in added]

    for sid in new:
        if sid in old:
            changes = {}
            for key in TRACKED_FIELDS:
                if norm(old[sid].get(key, "")) != norm(new[sid].get(key, "")):
                    changes[key] = (old[sid].get(key, ""), new[sid].get(key, ""))
            if changes:
                msgs.append(msg_changed(old[sid], new[sid], changes))

    for sid in old:
        if sid not in new:
            d = parse_date(old[sid].get("date", ""))
            if d is None or d >= today:  # sessions that simply passed are not news
                msgs.append(msg_removed(old[sid]))
            else:
                log.info("Past session left the calendar: %s", one_line(old[sid]))

    state["sessions"] = new
    return "ok", msgs


# --------------------------------------------------------------------------
# State file (atomic writes + backup)
# --------------------------------------------------------------------------
def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    bak = Path(str(path) + ".bak")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        try:
            shutil.copy2(path, bak)
        except OSError as e:
            log.warning("Could not update state backup: %s", e)
    for attempt in range(5):  # Windows antivirus may briefly lock the file
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    os.replace(tmp, path)


def load_state(path: Path = STATE_PATH) -> dict:
    path = Path(path)
    for p in (path, Path(str(path) + ".bak")):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
                raise ValueError("unexpected structure")
            st = default_state()
            st.update(data)
            if p != path:
                log.warning("Main state file was damaged; restored from backup.")
            return st
        except Exception as e:
            log.error("State file %s unreadable (%s).", p.name, e)
            try:
                p.rename(Path(str(p) + ".corrupt"))
            except OSError:
                pass
    return default_state()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass
class Config:
    token: str
    chat_id: str
    interval_minutes: int = 15
    error_after_failures: int = 2
    error_repeat_hours: float = 6.0
    use_playwright: str = "auto"
    telegram_proxy: str = ""
    site_proxy: str = ""
    heartbeat: bool = True
    timezone: str = "Asia/Tehran"


def load_config(path: Path = CONFIG_PATH) -> Config:
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    env_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    cp = configparser.ConfigParser()
    if Path(path).exists():
        cp.read(path, encoding="utf-8-sig")
    elif not (env_token and env_chat):
        raise ConfigError("config.ini not found. Run find_chat_id.bat first (it creates it for you), "
                          "or copy config.ini.example to config.ini and fill it in.")
    strip = lambda v: v.strip().strip("\"'").strip()
    token = env_token or strip(cp.get("telegram", "bot_token", fallback=""))
    chat = env_chat or strip(cp.get("telegram", "chat_id", fallback=""))
    if token in ("", "YOUR_BOT_TOKEN") or not re.fullmatch(r"\d+:[A-Za-z0-9_\-]{20,}", token):
        raise ConfigError("bot_token in config.ini is missing or invalid. Run find_chat_id.bat.")
    if chat in ("", "YOUR_CHAT_ID") or not re.fullmatch(r"-?\d+|@\w+", chat):
        raise ConfigError("chat_id in config.ini is missing or invalid. Run find_chat_id.bat.")

    def geti(sec, key, default, minimum):
        try:
            return max(minimum, int(cp.get(sec, key, fallback=str(default)).strip()))
        except ValueError:
            return default

    return Config(
        token=token, chat_id=chat,
        interval_minutes=geti("monitor", "interval_minutes", 15, 5),
        error_after_failures=geti("monitor", "error_after_failures", 2, 1),
        error_repeat_hours=float(geti("monitor", "error_repeat_hours", 6, 1)),
        use_playwright=cp.get("monitor", "use_playwright", fallback="auto").strip().lower(),
        telegram_proxy=strip(cp.get("network", "telegram_proxy", fallback="")),
        site_proxy=strip(cp.get("network", "site_proxy", fallback="")),
        heartbeat=(os.environ.get("CENTS_HEARTBEAT") or cp.get("monitor", "heartbeat", fallback="yes")
                   ).strip().lower() not in ("no", "false", "0", "off"),
        timezone=(os.environ.get("CENTS_TZ") or cp.get("monitor", "timezone", fallback="Asia/Tehran")).strip(),
    )


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str, proxy: str = ""):
        self.token, self.chat_id = token, chat_id
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def send(self, text: str, sleep: Callable = time.sleep, silent: bool = False):
        """Returns (ok, permanent_failure)."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text[:TELEGRAM_MAX], "disable_web_page_preview": True,
                   "disable_notification": bool(silent)}
        for _ in range(2):
            try:
                r = self.session.post(url, json=payload, timeout=(10, 30))
            except requests.RequestException as e:
                log.warning("Telegram unreachable: %s", redact(str(e), self.token))
                return False, False
            if r.status_code == 200:
                return True, False
            try:
                body = r.json()
            except ValueError:
                body = {}
            desc = body.get("description", r.text[:200])
            if r.status_code == 429:
                wait = min(int(body.get("parameters", {}).get("retry_after", 5)), 60)
                log.warning("Telegram rate limit; waiting %ss", wait)
                sleep(wait + 1)
                continue
            log.error("Telegram error %s: %s", r.status_code, redact(str(desc), self.token))
            return False, r.status_code == 400
        return False, False


# --------------------------------------------------------------------------
# Fetching the calendar
# --------------------------------------------------------------------------
def fetch_html(session, sleep: Callable = time.sleep, url: str = CALENDAR_URL) -> bytes:
    """Download the page: timeouts + up to 3 attempts for temporary errors."""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = session.get(url, timeout=(10, 30), headers={
                "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en"})
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 0
                if r.status_code == 429:
                    try:
                        wait = min(int(r.headers.get("Retry-After", "0")), 120)
                    except ValueError:
                        wait = 0
                last = requests.HTTPError(f"HTTP {r.status_code}")
                if attempt < 3:
                    sleep(max(wait, 5 * attempt * attempt))
                continue
            r.raise_for_status()
            host = urlparse(r.url).netloc.lower()
            if not host.endswith("testcisia.it"):
                raise ParseError(f"Redirected to unexpected host: {host}")
            return r.content
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            if attempt < 3:
                sleep(5 * attempt * attempt)
    raise RuntimeError(f"Could not download the calendar after 3 attempts: {last}")


def fetch_html_playwright() -> str:  # pragma: no cover (optional, needs extra install)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(CALENDAR_URL, wait_until="networkidle", timeout=60000)
            return page.content()
        finally:
            browser.close()


def make_fetcher(cfg: Config) -> Callable[[], list[dict]]:
    session = requests.Session()
    if cfg.site_proxy:
        session.proxies = {"http": cfg.site_proxy, "https": cfg.site_proxy}

    def fetch_records() -> list[dict]:
        html = fetch_html(session)
        try:
            return parse_calendar(html)
        except ParseError:
            if cfg.use_playwright == "no":
                raise
            try:
                import playwright  # noqa: F401
            except ImportError:
                raise
            log.info("Plain HTML had no calendar table; trying Playwright browser.")
            return parse_calendar(fetch_html_playwright())

    return fetch_records


# --------------------------------------------------------------------------
# The monitor
# --------------------------------------------------------------------------
class Monitor:
    def __init__(self, cfg: Config, tg, fetch_records: Callable[[], list[dict]],
                 state_path: Path = STATE_PATH, sleep: Callable = time.sleep):
        self.cfg, self.tg, self.fetch_records = cfg, tg, fetch_records
        self.state_path, self.sleep = state_path, sleep
        self.state = load_state(state_path)
        if self.state.get("url") != CALENDAR_URL:  # monitored page changed -> new baseline
            self.state = default_state()

    def save(self):
        save_state(self.state, self.state_path)

    def enqueue(self, texts, silent: bool = False):
        if isinstance(texts, str):
            texts = [texts]
        for t in texts:
            self.state["outbox"].append({"id": self.state["next_id"], "text": t, "tries": 0, "silent": silent})
            self.state["next_id"] += 1
        del self.state["outbox"][:-OUTBOX_LIMIT]

    def flush(self):
        """Send queued messages in order; unsent ones stay in the state file for the next run."""
        box = self.state["outbox"]
        first = True
        while box:
            if not first:
                self.sleep(1.1)  # Telegram allows about 1 message per second per chat
            first = False
            item = box[0]
            ok, permanent = self.tg.send(item["text"], silent=item.get("silent", False))
            if ok:
                box.pop(0)
                self.save()
                continue
            item["tries"] += 1
            if permanent and item["tries"] >= 3:
                log.error("Dropping a message Telegram keeps rejecting.")
                box.pop(0)
                self.save()
                continue
            self.save()
            break

    def _failure(self, err: Exception):
        st = self.state
        st["consecutive_failures"] += 1
        text = redact(str(err), self.cfg.token)[:300]
        log.error("Check failed (%d in a row): %s", st["consecutive_failures"], text)
        if st["consecutive_failures"] < self.cfg.error_after_failures:
            return
        last = st.get("last_error_notice")
        due = last is None or (datetime.now().astimezone() - datetime.fromisoformat(last)
                               ).total_seconds() >= self.cfg.error_repeat_hours * 3600
        if due:
            st["last_error_notice"] = now_iso()
            st["error_active"] = True
            self.enqueue("❌ بررسی تقویم CEnT-S ناموفق بود.\n"
                         f"تعداد تلاش‌های ناموفق پشت‌سرهم: {st['consecutive_failures']}\n"
                         f"علت: {text}\n"
                         "ربات تلاش می‌کند و اطلاعات قبلی حفظ شده است.\n"
                         "(این پیام حداکثر هر چند ساعت یک‌بار تکرار می‌شود.)")

    def run_cycle(self, startup: bool = False) -> bool:
        st = self.state
        was_initialized = st["initialized"]
        try:
            records = self.fetch_records()
        except Exception as e:
            self._failure(e)
            if startup:
                self.enqueue_first(msg_startup(self.cfg, st["sessions"], False, True)
                                   if not was_initialized else
                                   msg_startup(self.cfg, st["sessions"], False, False))
            self.save()
            self.flush()
            return False

        notices = []
        if st["error_active"]:
            notices.append("✅ اتصال به تقویم CEnT-S دوباره برقرار شد و بررسی‌ها عادی است.")
        st.update(consecutive_failures=0, error_active=False, last_error_notice=None)
        status, msgs = apply_check(st, records, date.today())
        log.info("Check OK: %d sessions, status=%s, %d notification(s)",
                 len(records), status, len(msgs))
        if status == "suspicious":
            log.warning("Page looks empty/much smaller than before (%d/%d); keeping old data.",
                        st["pending_empty"], EMPTY_CONFIRM)
        head = []
        if startup:
            head = msg_startup(self.cfg, st["sessions"], status == "baseline", False)
        self.save()  # state first, then send: a crash can never cause duplicate alerts
        self.enqueue(head + notices + msgs)
        self.save()
        self.flush()
        if (self.cfg.heartbeat and status == "ok" and not msgs and not head and not notices
                and not st["outbox"]):
            self.tg.send(msg_heartbeat(self.cfg, len(st["sessions"])), silent=True)  # silent, not queued
        return True

    def enqueue_first(self, texts):
        old = self.state["outbox"]
        self.state["outbox"] = []
        self.enqueue(texts)
        self.state["outbox"] += old


# --------------------------------------------------------------------------
# Program entry
# --------------------------------------------------------------------------
def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(ch)
    log.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def acquire_lock():
    fh = open(LOCK_PATH, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            if fh.tell() == 0 and os.path.getsize(LOCK_PATH) == 0:
                fh.write(" ")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="CISIA CEnT-S calendar monitor")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    args = ap.parse_args(argv)
    setup_logging()

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("Configuration problem: %s", e)
        return 2

    lock = acquire_lock()
    if lock is None:
        log.error("Another copy of the monitor is already running. Close it first.")
        return 3

    tg = Telegram(cfg.token, cfg.chat_id, cfg.telegram_proxy)
    if args.test_telegram:
        ok, _ = tg.send("✅ پیام آزمایشی: اتصال ربات CEnT-S به تلگرام درست کار می‌کند.")
        log.info("Test message %s", "sent" if ok else "FAILED (see messages above)")
        return 0 if ok else 1

    mon = Monitor(cfg, tg, make_fetcher(cfg))
    log.info("Monitoring %s every %d minutes. Press Ctrl+C to stop.", CALENDAR_URL, cfg.interval_minutes)
    try:
        mon.run_cycle(startup=(not args.once) or (not mon.state["initialized"]))
        if args.once:
            return 0
        while True:
            time.sleep(cfg.interval_minutes * 60 + random.uniform(0, 30))
            try:
                mon.run_cycle()
            except Exception:
                log.exception("Unexpected error during a check (will retry next time)")
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
