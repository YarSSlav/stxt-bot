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
        soup = BeautifulSoup(response.text, 'html.parser')
        groups = [h3.text.strip() for h3 in soup.find_all('h3') if h3.text.strip()]
        return groups if groups else ["МАШ24", "КН25", "АКТ26"]
    except Exception:
        return ["МАШ24"]

def get_schedule_for_day(group, day_name):
    url = "https://www.stxt.com.ua/schedule"
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Ищем заголовок нужной группы
        group_header = None
        for tag in soup.find_all(['h2', 'h3']):
            if group in tag.text:
                group_header = tag
                break
                
        if not group_header: 
            return []
            
        # Собираем все текстовые строки под заголовком группы до следующей группы
        lines = []
        for sib in group_header.find_next_siblings():
            # Если встретили заголовок следующей группы, останавливаемся
            if sib.name in ['h2', 'h3']: 
                break
            
            # Извлекаем чистый текст из всех вложенных тегов
            for text_node in sib.find_all(string=True):
                clean_text = text_node.strip()
                if clean_text:
                    lines.append(clean_text)
                    
        # Парсим строки
        lessons = []
        is_target_day = False
        current_pair = None
        days_of_week = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"]
        
        for line in lines:
            # Если это название дня недели
            if line in days_of_week:
                if line == day_name:
                    is_target_day = True
                else:
                    is_target_day = False
                current_pair = None
                continue
                
            if is_target_day:
                # Если строка - это номер пары (1-7)
                if line.isdigit() and 1 <= int(line) <= 7:
                    current_pair = int(line)
                elif current_pair is not None:
                    # Если это название предмета или препод
                    pair_exists = False
                    for l in lessons:
                        if l['pair'] == current_pair:
                            l['info'] += f" {line}"
                            pair_exists = True
                            break
                    if not pair_exists:
                        lessons.append({"pair": current_pair, "info": line})
                        
        return lessons
    except Exception as e:
        print(f"Ошибка парсинга расписания: {e}")
        return []

# --- Функция замен (осталась без изменений, она отлично работала) ---
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
