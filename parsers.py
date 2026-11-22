import asyncio
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from io import BytesIO

import openpyxl
import requests
from bs4 import BeautifulSoup, NavigableString, Comment

try:
    import olefile
except ImportError:  # необов'язкова залежність (старі .doc із замінами)
    olefile = None

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # на Windows без tzdata
    _TZ = None


# ───────────────────────── Налаштування ─────────────────────────

SCHEDULE_URL = "https://www.stxt.com.ua/schedule"
SUBS_URL = "https://www.stxt.com.ua/download/zam.php"
BUS_URL = "https://smila.biz/bus"

HEADERS = {"User-Agent": "Mozilla/5.0"}
HTTP_TIMEOUT = 15

# Скільки секунд тримаємо сторінку в кеші
TTL_SCHEDULE = 600   # 10 хв
TTL_SUBS = 180       # 3 хв
TTL_BUS = 1800       # 30 хв

# 7-ї пари тут немає: час перевір на https://www.stxt.com.ua/zv_schedule
# і додай рядок виду  7: "17:30–18:50",
BELL_SCHEDULE = {
    1: "08:20–09:40",
    2: "09:50–11:10",
    3: "11:20–12:40",
    4: "13:00–14:20",
    5: "14:30–15:50",
    6: "16:00–17:20",
}

DAYS = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"]
FALLBACK_GROUPS = ["МАШ24", "КН25", "АКТ26"]


# ───────────────────────── Мережа + кеш ─────────────────────────

_cache = {}  # url -> (час, response)


def _cached_get(url, ttl):
    """GET з кешем. Якщо сайт не відповів, а старий кеш є, повертає старий."""
    now = time.time()
    item = _cache.get(url)
    if item and now - item[0] < ttl:
        return item[1]
    try:
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        r.encoding = "utf-8"
        _cache[url] = (now, r)
        return r
    except Exception:
        if item:
            return item[1]
        raise


def _norm(s):
    s = s.replace("\xa0", " ").replace("’", "'").replace("`", "'").replace("ʼ", "'")
    return re.sub(r"\s+", " ", s).strip()


DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")


def _extract_dates(text):
    """Усі дати виду дд.мм.рррр, знайдені в тексті файлу замін."""
    found = set()
    for d, m, y in DATE_RE.findall(text or ""):
        y = int(y)
        y = y + 2000 if y < 100 else y
        try:
            found.add(date(y, int(m), int(d)))
        except ValueError:
            pass
    return found


def _soup(url, ttl):
    return BeautifulSoup(_cached_get(url, ttl).text, "html.parser")


# ───────────────────────── Розклад занять ─────────────────────────

GROUP_RE = re.compile(r"^[А-ЯІЇЄA-Z]{2,4}\d{2}( \+ [А-ЯІЇЄA-Z]{2,4}\d{2})?$")


def get_groups():
    try:
        soup = _soup(SCHEDULE_URL, TTL_SCHEDULE)
        groups = [_norm(h.get_text()) for h in soup.find_all("h3")]
        groups = [g for g in groups if GROUP_RE.match(g)]
        return groups or FALLBACK_GROUPS
    except Exception:
        return FALLBACK_GROUPS


def _group_lines(soup, group):
    """Текстові рядки між <h3>групи</h3> і наступним <h3>."""
    for h3 in soup.find_all("h3"):
        if _norm(h3.get_text()) != group:
            continue
        lines = []
        skip_own_text = True
        for el in h3.next_elements:
            if el is h3:
                continue
            if getattr(el, "name", None) in ("h3", "h6", "footer"):
                break
            if isinstance(el, NavigableString) and not isinstance(el, Comment):
                if el.parent is not None and el.parent.name in ("script", "style"):
                    continue
                t = _norm(str(el))
                if not t:
                    continue
                if skip_own_text and t == group:  # сам заголовок h3
                    skip_own_text = False
                    continue
                lines.append(t)
        return lines
    raise ValueError(f"Групу {group} не знайдено на сторінці розкладу")


def _parse_day(group, day_name):
    """Повертає список пар дня. Кидає виняток, якщо сайт недоступний."""
    lines = _group_lines(_soup(SCHEDULE_URL, TTL_SCHEDULE), group)

    lessons = []
    in_day = False
    pair, info = None, []

    def flush():
        if pair is not None and info:
            lessons.append({"pair": pair, "info": " | ".join(info)})

    for line in lines:
        if line in DAYS:
            if in_day:
                break
            in_day = (line == day_name)
            continue
        if not in_day:
            continue
        if re.fullmatch(r"[1-7]", line):  # номер пари
            flush()
            pair, info = int(line), []
        elif pair is not None:
            info.append(line)             # предмет / викладач
    if in_day:
        flush()
    return lessons


def get_schedule_for_day(group, day_name):
    try:
        return _parse_day(group, day_name)
    except Exception as e:
        print(f"Помилка парсингу розкладу: {e}")
        return []


# ───────────────────────── Заміни ─────────────────────────

def _fill_from_row(cells, group, state, added, removed):
    """Спільна обробка одного рядка таблиці замін."""
    if len(cells) < 2:
        return
    if cells[0].isdigit():
        state["pair"] = cells[0]
    if group in cells[1] and state["pair"]:
        subject = cells[3] if len(cells) > 3 else ""
        last_col = cells[-1]
        if "ЗНЯТО" in subject.upper() or "ЗНЯТО" in last_col.upper():
            removed.append(state["pair"])
        else:
            added[state["pair"]] = subject


def get_substitutions(group):
    """
    Повертає {"added": {...}, "removed": [...], "dates": {date, ...}}.
    "dates" - усі дати, знайдені у файлі замін (порожньо, якщо дат немає).
    """
    added, removed, dates = {}, [], set()

    try:
        content = _cached_get(SUBS_URL, TTL_SUBS).content

        # 1) HTML-таблиця
        if b"<table" in content[:1000].lower():
            soup = BeautifulSoup(content, "html.parser")
            dates = _extract_dates(soup.get_text(" "))
            state = {"pair": None}
            for tr in soup.find_all("tr"):
                cells = [td.text.strip() for td in tr.find_all(["td", "th"])]
                _fill_from_row(cells, group, state, added, removed)
            return {"added": added, "removed": removed, "dates": dates}

        # 2) .docx
        try:
            with zipfile.ZipFile(BytesIO(content)) as z:
                if "word/document.xml" in z.namelist():
                    root = ET.fromstring(z.read("word/document.xml"))
                    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                    dates = _extract_dates(" ".join(t.text for t in root.findall(".//w:t", ns) if t.text))
                    state = {"pair": None}
                    for tr in root.findall(".//w:tr", ns):
                        cells = []
                        for tc in tr.findall(".//w:tc", ns):
                            text = "".join(t.text for t in tc.findall(".//w:t", ns) if t.text)
                            cells.append(text.strip())
                        _fill_from_row(cells, group, state, added, removed)
                    return {"added": added, "removed": removed, "dates": dates}
        except zipfile.BadZipFile:
            pass

        # 3) .xlsx
        try:
            wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
            state = {"pair": None}
            all_text = []
            for row in wb.active.iter_rows(values_only=True):
                if not row or len(row) < 2:
                    continue
                cells = [str(c).strip() if c is not None else "" for c in row]
                all_text.append(" ".join(cells))
                _fill_from_row(cells, group, state, added, removed)
            dates = _extract_dates(" ".join(all_text))
            return {"added": added, "removed": removed, "dates": dates}
        except Exception:
            pass

        # 4) старий .doc
        if olefile is not None:
            try:
                ole = olefile.OleFileIO(BytesIO(content))
                if ole.exists("WordDocument"):
                    text = ole.openstream("WordDocument").read().decode("utf-16-le", errors="ignore")
                    text = text.replace("\r", " ").replace("\n", " ")
                    dates = _extract_dates(text)
                    cells = text.split("\x07")
                    current_pair = None
                    for i, cell in enumerate(cells):
                        cell = cell.strip()
                        if not cell:
                            continue
                        if cell.isdigit() and len(cell) <= 2 and int(cell) < 10:
                            current_pair = cell
                        if group in cell and current_pair:
                            if "\x00" in cell:
                                break
                            subject = cells[i + 2].strip() if i + 2 < len(cells) else ""
                            teacher = cells[i + 3].strip() if i + 3 < len(cells) else ""
                            last_col = cells[i + 4].strip() if i + 4 < len(cells) else ""
                            if "ЗНЯТО" in (subject + teacher + last_col).upper():
                                removed.append(current_pair)
                            elif subject:
                                added[current_pair] = f"{subject} - {teacher}" if teacher else subject
                    return {"added": added, "removed": removed, "dates": dates}
            except Exception:
                pass

    except Exception as e:
        print(f"Помилка завантаження замін: {e}")

    return {"added": added, "removed": removed, "dates": dates}


# ───────────────────────── Автобуси ─────────────────────────

def get_bus_routes():
    return ["3", "4", "5", "17", "30", "32", "34", "39", "40", "48", "49", "150", "152", "302"]


def get_bus_schedule(route_num):
    try:
        text = _soup(BUS_URL, TTL_BUS).get_text()
        pattern = re.compile(rf"№\s*{re.escape(str(route_num))}\s*\|(.*?)(?=№|\Z)", re.DOTALL)
        match = pattern.search(text)
        if match:
            schedule_text = match.group(1).strip()
            return f"🚌 Автобус №{route_num}\n\n{schedule_text[:800]}..."
        return f"Розклад для автобуса №{route_num} не знайдено."
    except Exception as e:
        return f"Помилка: {e}"


# ───────────────────────── Збірка повідомлення ─────────────────────────

def _now():
    return datetime.now(_TZ) if _TZ else datetime.now()


def get_day_name(offset_days=0):
    """Назва дня тижня для сьогодні/завтра. Для неділі повертає None."""
    d = _now() + timedelta(days=offset_days)
    wd = d.weekday()  # 0 = понеділок … 6 = неділя
    return DAYS[wd] if wd < len(DAYS) else None


def build_day_message(group, day_name=None):
    """
    Розклад дня + заміни, об'єднані в одне повідомлення.
    Якщо day_name не вказано, береться сьогодні.
    """
    if day_name is None:
        day_name = get_day_name()
    if day_name is None:
        return f"📅 {group}\n\nУ неділю пар немає 🎉"

    try:
        lessons = _parse_day(group, day_name)
    except Exception as e:
        print(f"Помилка розкладу: {e}")
        return "⚠️ Не вдалося завантажити розклад із сайту коледжу. Спробуй ще раз пізніше."

    subs = get_substitutions(group)

    by_pair = {l["pair"]: l["info"] for l in lessons}
    changed = set()

    for p in subs["removed"]:
        if p.isdigit():
            by_pair.pop(int(p), None)
            changed.add(int(p))
    for p, subject in subs["added"].items():
        if p.isdigit() and subject:
            by_pair[int(p)] = subject
            changed.add(int(p))

    if not by_pair:
        return f"📅 {group} — {day_name}\n\nПар немає 🎉"

    out = [f"📅 {group} — {day_name}", ""]
    for p in sorted(by_pair):
        time_str = BELL_SCHEDULE.get(p)
        head = f"{p}) {time_str}" if time_str else f"{p})"
        mark = "🔄 " if p in changed else ""
        out.append(f"{head}\n   {mark}{by_pair[p]}")
    if changed:
        out.append("\n🔄 — пара змінена згідно із замінами")
    return "\n".join(out)


# ───────────────────────── Async-обгортки для бота ─────────────────────────
# requests блокуючий, тому в async-боті (aiogram / python-telegram-bot v20+)
# викликай саме ці функції.

async def build_day_message_async(group, day_name=None):
    return await asyncio.to_thread(build_day_message, group, day_name)


async def get_bus_schedule_async(route_num):
    return await asyncio.to_thread(get_bus_schedule, route_num)


async def get_groups_async():
    return await asyncio.to_thread(get_groups)
