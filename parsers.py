import requests
from bs4 import BeautifulSoup
from docx import Document
import openpyxl
import olefile
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
import re

BELL_SCHEDULE = {
    1: "08:20–09:40",
    2: "09:50–11:10",
    3: "11:20–12:40",
    4: "13:00–14:20",
    5: "14:30–15:50",
    6: "16:00–17:20"
}

def get_groups():
    url = "https://www.stxt.com.ua/schedule"
    try:
        response = requests.get(url)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        groups = [h3.text.strip() for h3 in soup.find_all('h3') if h3.text.strip()]
        return groups if groups else ["МАШ24", "КН25", "АКТ26"]
    except Exception:
        return ["МАШ24"]

def get_schedule_for_day(group, day_name):
    url = "https://www.stxt.com.ua/schedule"
    try:
        response = requests.get(url)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. Получаем ВЕСЬ текст страницы, разбитый на строки
        text = soup.get_text(separator='\n', strip=True)
        lines = [line.replace('\xa0', ' ').strip() for line in text.split('\n') if line.strip()]
        
        all_groups = get_groups()
        start_idx = -1
        end_idx = -1
        
        # 2. Находим, где начинается наша группа и где заканчивается
        for i, line in enumerate(lines):
            if re.search(rf'\b{re.escape(group)}\b', line):
                start_idx = i
                break
                
        if start_idx == -1: return []

        for i in range(start_idx + 1, len(lines)):
            if lines[i] in all_groups:
                end_idx = i
                break
        
        if end_idx == -1: end_idx = len(lines)
            
        group_lines = lines[start_idx:end_idx]
        
        # 3. Идем по строчкам и выуживаем пары
        days = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"]
        is_target_day = False
        current_pair = None
        current_info = []
        lessons = []
        
        for line in group_lines:
            # Если нашли день недели
            if line in days:
                if is_target_day:
                    if current_pair is not None and current_info:
                        lessons.append({"pair": current_pair, "info": " ".join(current_info)})
                    break 
                is_target_day = (line == day_name)
                continue
                
            # Если мы внутри нужного дня
            if is_target_day:
                # Проверяем, начинается ли строка с номера пары (например, "4", "4.", "4 Українська мова")
                match = re.match(r'^([1-7])\b[\.\s]*(.*)', line)
                if match:
                    if current_pair is not None and current_info:
                        lessons.append({"pair": current_pair, "info": " ".join(current_info)})
                    current_pair = int(match.group(1))
                    current_info = [match.group(2).strip()] if match.group(2).strip() else []
                elif current_pair is not None:
                    # Название предмета или преподаватель
                    current_info.append(line)
                    
        # Сохраняем самую последнюю пару, если день закончился
        if is_target_day and current_pair is not None and current_info:
            lessons.append({"pair": current_pair, "info": " ".join(current_info)})
            
        return lessons
    except Exception as e:
        print(f"Ошибка парсинга расписания: {e}")
        return []

# --- Функция замен (осталась без изменений, она работает идеально) ---
def get_substitutions(group):
    url = "https://www.stxt.com.ua/download/zam.php"
    added, removed = {}, []
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        response = requests.get(url, headers=headers)
        content = response.content

        if b"<table" in content[:1000].lower():
            soup = BeautifulSoup(content, 'html.parser')
            current_pair = None
            for tr in soup.find_all('tr'):
                cells = [td.text.strip() for td in tr.find_all(['td', 'th'])]
                if len(cells) < 2: continue
                if cells[0].isdigit(): current_pair = cells[0]
                if group in cells[1] and current_pair:
                    subject = cells[3] if len(cells) > 3 else ""
                    last_col = cells[-1] if len(cells) > 0 else ""
                    if "ЗНЯТО" in subject.upper() or "ЗНЯТО" in last_col.upper():
                        removed.append(current_pair)
                    else:
                        added[current_pair] = subject
            return {"added": added, "removed": removed}

        try:
            with zipfile.ZipFile(BytesIO(content)) as z:
                if 'word/document.xml' in z.namelist():
                    xml_content = z.read('word/document.xml')
                    root = ET.fromstring(xml_content)
                    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                    current_pair = None
                    for tr in root.findall('.//w:tr', ns):
                        cells = []
                        for tc in tr.findall('.//w:tc', ns):
                            text = "".join([t.text for t in tc.findall('.//w:t', ns) if t.text])
                            cells.append(text.strip())
                        if len(cells) >= 2:
                            if cells[0].isdigit(): current_pair = cells[0]
                            if group in cells[1] and current_pair:
                                subject = cells[3] if len(cells) > 3 else ""
                                last_col = cells[-1] if len(cells) > 0 else ""
                                if "ЗНЯТО" in subject.upper() or "ЗНЯТО" in last_col.upper():
                                    removed.append(current_pair)
                                else:
                                    added[current_pair] = subject
                    return {"added": added, "removed": removed}
        except zipfile.BadZipFile:
            pass

        try:
            wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
            current_pair = None
            for row in wb.active.iter_rows(values_only=True):
                if not row or len(row) < 2: continue
                cells = [str(c).strip() if c is not None else "" for c in row]
                if cells[0].isdigit(): current_pair = cells[0]
                if group in cells[1] and current_pair:
                    subject = cells[3] if len(cells) > 3 else ""
                    last_col = cells[-1] if len(cells) > 0 else ""
                    if "ЗНЯТО" in subject.upper() or "ЗНЯТО" in last_col.upper():
                        removed.append(current_pair)
                    else:
                        added[current_pair] = subject
            return {"added": added, "removed": removed}
        except Exception:
            pass

        try:
            ole = olefile.OleFileIO(BytesIO(content))
            if ole.exists('WordDocument'):
                text = ole.openstream('WordDocument').read().decode('utf-16-le', errors='ignore')
                text = text.replace('\r', ' ').replace('\n', ' ')
                cells = text.split('\x07')
                current_pair = None
                for i, cell in enumerate(cells):
                    cell = cell.strip()
                    if not cell: continue
                    if cell.isdigit() and len(cell) <= 2 and int(cell) < 10:
                        current_pair = cell
                    if group in cell and current_pair:
                        if '\x00' in cell: break 
                        subject = cells[i+2].strip() if i+2 < len(cells) else ""
                        teacher = cells[i+3].strip() if i+3 < len(cells) else ""
                        last_col = cells[i+4].strip() if i+4 < len(cells) else ""
                        if "ЗНЯТО" in subject.upper() or "ЗНЯТО" in teacher.upper() or "ЗНЯТО" in last_col.upper():
                            removed.append(current_pair)
                        else:
                            if subject:
                                info = subject
                                if teacher: info += f" - {teacher}"
                                added[current_pair] = info
                return {"added": added, "removed": removed}
        except Exception:
            pass
            
    except Exception as e:
        print(f"Ошибка скачивания замен: {e}")
        
    return {"added": added, "removed": removed}

def get_bus_routes():
    return ["3", "4", "5", "17", "30", "32", "34", "39", "40", "48", "49", "150", "152", "302"]

def get_bus_schedule(route_num):
    url = "https://smila.biz/bus"
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text()
        pattern = re.compile(rf"№\s*{route_num}\s*\|(.*?)(?=№|\Z)", re.DOTALL)
        match = pattern.search(text)
        if match:
            schedule_text = match.group(1).strip()
            return f"🚌 Автобус №{route_num}\n\n{schedule_text[:800]}..."
        return f"Расписание для автобуса №{route_num} не найдено."
    except Exception as e:
        return f"Ошибка: {e}"
