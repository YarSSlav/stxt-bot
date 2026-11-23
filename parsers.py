import asyncio
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from html import escape
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

# ───────────────────────── Тиждень (перший / другий) ─────────────────────────
# У файлі замін вказано, який зараз тиждень. Перший тиждень = верхній рядок
# у розкладі, другий = нижній. Пари з нижнього рядка на першому тижні не бувають
# (і навпаки).

# Пари, які бувають лише на одному тижні, але на сайті це ніяк не позначено.
# Ключ: (група, день, номер пари). Значення: 1 - лише перший тиждень, 2 - лише другий.
# Щоб додати ще одну пару, допиши рядок виду  ("КН24", "Четвер", 3): 2,
WEEK_ONLY = {
    ("МАШ24", "Четвер", 2): 2,   # Астрономія (вибіркова) - нижній рядок
    ("МАШ24", "Четвер", 3): 2,   # Астрономія (вибіркова) - нижній рядок
}

# Позначка на сайті: «Астрономія (по 2 тижню) (66)»
WEEK_MARK_RE = re.compile(r"\s*\(\s*по\s*([12])\s*тижню\s*\)", re.IGNORECASE)
_TEACHER_RE = re.compile(r"^[А-ЯІЇЄҐ][а-яіїєґ'-]+\s+[А-ЯІЇЄҐ]\.\s?[А-ЯІЇЄҐ]\.(\s*\([^)]*\))?$")

_WEEK_WORD = r"(?P<w>перш\w*|друг\w*|(?<![\w.])(?-i:[IІ]{1,2})(?![\w.])|(?<![\w.])[12](?![\w.]))"
_WEEK_RES = (
    re.compile(r"тиж\w*\s*[:\-–—]?\s*" + _WEEK_WORD, re.IGNORECASE),
    re.compile(r"(?<!по )" + _WEEK_WORD + r"\s*тиж", re.IGNORECASE),
)


def _week_value(w):
    w = w.lower()
    if w.startswith("перш") or w in ("i", "і", "1"):
        return 1
    if w.startswith("друг") or w in ("ii", "іі", "2"):
        return 2
    return None


def _extract_week(text):
    """«Тиждень: перший» / «II тиждень» -> 1 або 2 (None, якщо у файлі не вказано)."""
    text = _norm(text or "")
    for rx in _WEEK_RES:
        m = rx.search(text)
        if m:
            v = _week_value(m.group("w"))
            if v:
                return v
    return None


def _extract_meta(text):
    return _extract_dates(text), _extract_week(text)


def week_for_date(file_week, file_dates, target_date):
    """
    Тиждень на потрібну дату. Файл замін відноситься до своїх дат; якщо потрібна
    дата в іншому тижні, парність перевертається кожні 7 днів.
    """
    if file_week not in (1, 2):
        return None
    if not file_dates:
        return file_week
    ref = min(file_dates)
    diff = (target_date - timedelta(days=target_date.weekday())) - (ref - timedelta(days=ref.weekday()))
    return file_week if (diff.days // 7) % 2 == 0 else 3 - file_week


def filter_by_week(group, day_name, lessons, week):
    """Прибирає пари (або їхні частини), яких немає на цьому тижні. Без тижня нічого не чіпає."""
    if week not in (1, 2):
        return lessons
    out = []
    for l in lessons:
        need = WEEK_ONLY.get((group, day_name, l["pair"]))
        if need and need != week:
            continue
        kept, dropped = [], False
        for part in l["info"].split(" | "):
            m = WEEK_MARK_RE.search(part)
            if m:
                if int(m.group(1)) != week:
                    dropped = True
                    continue
                part = WEEK_MARK_RE.sub("", part, count=1).strip()
            kept.append(part)
        # якщо прибрали предмет, а лишились самі викладачі, пари на цьому тижні немає
        if not kept or (dropped and all(_TEACHER_RE.match(p) for p in kept)):
            continue
        out.append({"pair": l["pair"], "info": " | ".join(kept)})
    return out


# ───────────────────────── Аудиторії ─────────────────────────
# На сайті аудиторія стоїть у дужках: «Інформатика (70)», «Хімія (дист)»,
# «Виховна година (а/з)», а в підгруп - біля викладача: «Зінченко Т.П. (75)».

ROOM_RE = re.compile(r"^(?:ауд\.?\s*)?(\d{1,3}[А-Яа-яІіA-Za-z]?|[АСас]/[Зз]|дист\.?)$", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"^(.*?\S)?\s*\(([^()]+)\)\s*$")


def _clean_room(s):
    return re.sub(r"^ауд\.?\s*", "", s.strip(), flags=re.IGNORECASE).rstrip(".")


def split_trailing_room(text):
    """«Інформатика (70)» -> («Інформатика», «70»). Якщо в дужках не аудиторія -> (text, None)."""
    text = (text or "").strip()
    m = _TRAILING_PAREN_RE.match(text)
    if m and ROOM_RE.match(m.group(2).strip()):
        return (m.group(1) or "").strip(), _clean_room(m.group(2))
    return text, None


def room_label(room):
    r = room.lower()
    if r.startswith("дист"):
        return "дистанційно"
    if r == "а/з":
        return "актовий зал"
    if r == "с/з":
        return "спортивний зал"
    return f"ауд. {room}"


def _room_from_cell(s):
    """Комірка таблиці замін, що містить лише аудиторію (інакше None)."""
    s = (s or "").strip()
    if not s or "ЗНЯТО" in s.upper() or not ROOM_RE.match(s):
        return None
    return _clean_room(s)


def _fill_from_row(cells, group, state, added, removed):
    """Спільна обробка одного рядка таблиці замін."""
    if len(cells) < 2:
        return
    if cells[0].isdigit():
        state["pair"] = cells[0]
    if group in cells[1] and state["pair"]:
        subject = cells[3] if len(cells) > 3 else ""
        last_col = cells[-1]
        if "raw" in state:
            state["raw"].append(cells)
        if "ЗНЯТО" in subject.upper() or "ЗНЯТО" in last_col.upper():
            removed.append(state["pair"])
        else:
            # аудиторія із замін: коротка комірка (70, дист, а/з ...), крім номера пари, групи та предмета
            room = next((r for r in (_room_from_cell(c) for i, c in enumerate(cells)
                                     if i >= 2 and i != 3) if r), None)
            if room and subject and split_trailing_room(subject)[1] is None:
                subject = f"{subject} ({room})"
            added[state["pair"]] = subject


def get_substitutions(group):
    """
    Повертає {"added": {...}, "removed": [...], "dates": {date, ...}}.
    "dates" - усі дати, знайдені у файлі замін (порожньо, якщо дат немає).
    """
    added, removed, dates = {}, [], set()
    raw_rows = []  # сирі рядки замін для /debug
    week = None    # 1 / 2, якщо у файлі замін вказано тиждень

    try:
        content = _cached_get(SUBS_URL, TTL_SUBS).content

        # 1) HTML-таблиця
        if b"<table" in content[:1000].lower():
            soup = BeautifulSoup(content, "html.parser")
            dates, week = _extract_meta(soup.get_text(" "))
            state = {"pair": None, "raw": raw_rows}
            for tr in soup.find_all("tr"):
                cells = [td.text.strip() for td in tr.find_all(["td", "th"])]
                _fill_from_row(cells, group, state, added, removed)
            return {"added": added, "removed": removed, "dates": dates, "raw": raw_rows, "week": week}

        # 2) .docx
        try:
            with zipfile.ZipFile(BytesIO(content)) as z:
                if "word/document.xml" in z.namelist():
                    root = ET.fromstring(z.read("word/document.xml"))
                    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                    dates, week = _extract_meta(" ".join(t.text for t in root.findall(".//w:t", ns) if t.text))
                    state = {"pair": None, "raw": raw_rows}
                    for tr in root.findall(".//w:tr", ns):
                        cells = []
                        for tc in tr.findall(".//w:tc", ns):
                            text = "".join(t.text for t in tc.findall(".//w:t", ns) if t.text)
                            cells.append(text.strip())
                        _fill_from_row(cells, group, state, added, removed)
                    return {"added": added, "removed": removed, "dates": dates, "raw": raw_rows, "week": week}
        except zipfile.BadZipFile:
            pass

        # 3) .xlsx
        try:
            wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
            state = {"pair": None, "raw": raw_rows}
            all_text = []
            for row in wb.active.iter_rows(values_only=True):
                if not row or len(row) < 2:
                    continue
                cells = [str(c).strip() if c is not None else "" for c in row]
                all_text.append(" ".join(cells))
                _fill_from_row(cells, group, state, added, removed)
            dates, week = _extract_meta(" ".join(all_text))
            return {"added": added, "removed": removed, "dates": dates, "raw": raw_rows, "week": week}
        except Exception:
            pass

        # 4) старий .doc
        if olefile is not None:
            try:
                ole = olefile.OleFileIO(BytesIO(content))
                if ole.exists("WordDocument"):
                    text = ole.openstream("WordDocument").read().decode("utf-16-le", errors="ignore")
                    text = text.replace("\r", " ").replace("\n", " ")
                    dates, week = _extract_meta(text)
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
                            raw_rows.append([c.strip() for c in cells[i:i + 5]])
                            if "ЗНЯТО" in (subject + teacher + last_col).upper():
                                removed.append(current_pair)
                            elif subject:
                                room = _room_from_cell(teacher) or _room_from_cell(last_col)
                                if _room_from_cell(teacher):
                                    teacher = ""
                                if room and split_trailing_room(subject)[1] is None:
                                    subject = f"{subject} ({room})"
                                added[current_pair] = f"{subject} | {teacher}" if teacher else subject
                    return {"added": added, "removed": removed, "dates": dates, "raw": raw_rows, "week": week}
            except Exception:
                pass

    except Exception as e:
        print(f"Помилка завантаження замін: {e}")

    return {"added": added, "removed": removed, "dates": dates, "raw": raw_rows, "week": week}


# ───────────────────────── Автобуси ─────────────────────────

def get_bus_routes():
    return ["3", "4", "5", "17", "30", "32", "34", "39", "40", "48", "49", "150", "152", "302"]


BUS_HEAD_RE = re.compile(r"^№\s*(\d+|\.{3})\s*\|\s*(.*)$")
BUS_TIME_RE = re.compile(r"\d{1,2}[:.]\d{2}")
BUS_ENTRY_RE = re.compile(r"(\d{1,2})[:.](\d{2})([*#]*)\s*(\([^)|]*\)?)?")
BUS_NOTE_RE = re.compile(r"^(рейс|\*|#|\\#|крім|з \d|тел)", re.IGNORECASE)
BUS_STOP_WORDS = ("МАРШРУТИ", "ПЕРЕВІРЕНО", "АВТОПОСЛУГИ")
TG_LIMIT = 3900          # запас до ліміту Telegram (4096)

_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "tr", "table", "section",
               "h1", "h2", "h3", "h4", "h5", "h6", "hr"]


def _page_lines(url, ttl):
    """Текст сторінки по рядках, як його бачить людина (<br> і блоки дають перенос)."""
    soup = _soup(url, ttl)
    for t in soup(["script", "style"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert(0, "\n")
        tag.append("\n")
    lines = (_norm(l) for l in soup.get_text().split("\n"))
    return [l for l in lines if l]


def _bus_block(lines, route):
    """Знаходить блок маршруту. Пропускає рядки зі списку вкладок (без розкладу)."""
    route = str(route)
    for i, line in enumerate(lines):
        m = BUS_HEAD_RE.match(line)
        if not m or m.group(1) != route:
            continue
        block = []
        for nxt in lines[i + 1:]:
            if BUS_HEAD_RE.match(nxt) or any(w in nxt.upper() for w in BUS_STOP_WORDS):
                break
            block.append(nxt)
        if any(b.startswith("Відправлення") for b in block):
            return m.group(2), block
    return None, None


def _split_heading(line):
    """«Відправлення з АС: 06:30 | ...» -> ("Відправлення з АС", "06:30 | ...")."""
    m = re.search(r":(?!\d)", line)
    if m:
        return line[:m.start()].strip(), line[m.end():].strip()
    m = BUS_TIME_RE.search(line)
    if m:
        return line[:m.start()].strip(), line[m.start():].strip()
    return line, ""


def _render_times(line):
    """Рядок із часами -> список рядків (HTML): мітка (якщо є) і часи через « | »."""
    m = BUS_TIME_RE.search(line)
    if not m:
        return [escape(line)]

    res = []
    label = line[:m.start()].strip().rstrip(":").strip()
    if label:
        res.append(f"<i>{escape(label)}</i>")

    entries = []
    for h, mm, mark, annot in BUS_ENTRY_RE.findall(line[m.start():]):
        entry = f"{int(h):02d}:{mm}{mark}"
        annot = annot.strip()
        if annot:
            if not annot.endswith(")"):
                annot += ")"
            entry += f" {annot}"
        entries.append(entry)
    res.append(escape(" | ".join(entries)))
    return res


def _format_bus(route, title, block):
    out = [f"🚌 <b>Автобус №{escape(route)}</b>", ""]
    if title:
        out.append(f"<b>Маршрут:</b> {escape(title)}")

    notes, in_notes = [], False
    for line in block:
        if in_notes or BUS_NOTE_RE.match(line):
            in_notes = True
            notes.append(line)
        elif line.startswith("Відправлення"):
            head, rest = _split_heading(line)
            out.append("")
            out.append(f"• <b>{escape(head)}:</b>")
            if rest:
                out.extend(_render_times(rest))
        else:
            out.extend(_render_times(line))

    if notes:
        out.append("")
        for n in notes:
            m = re.match(r"^([*#\\]+)\s*(.*)$", n)
            if m:  # пояснення до позначок біля часів: «* — крім НД...»
                mark = m.group(1).replace("\\", "")
                out.append(f"ℹ️ {escape(mark)} — {escape(m.group(2))}")
            elif n.lower().startswith("рейс"):
                out.append(f"🗓 {escape(n)}")
            else:
                out.append(f"ℹ️ {escape(n)}")

    # Ліміт Telegram: ріжемо по рядках, щоб не поламати HTML-теги
    text, result = "", []
    for ln in out:
        if len(text) + len(ln) + 1 > TG_LIMIT:
            result.append("…")
            break
        result.append(ln)
        text += ln + "\n"
    return "\n".join(result)


def get_bus_schedule(route_num):
    """Повертає розклад автобуса у форматі Telegram HTML."""
    try:
        title, block = _bus_block(_page_lines(BUS_URL, TTL_BUS), route_num)
        if block is None:
            return f"Розклад для автобуса №{escape(str(route_num))} не знайдено."
        return _format_bus(str(route_num), title, block)
    except Exception as e:
        return f"Помилка: {escape(str(e))}"


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
