"""질문에 포함된 숫자 표를 기존 CSV 가져오기 경로에 연결한다."""
import csv
import io
import math
import re


def question_csv(question):
    lines = question.splitlines()
    for index in range(len(lines) - 2):
        if not lines[index].strip().startswith('|'):
            continue
        header = [cell.strip() for cell in lines[index].strip().strip('|').split('|')]
        separators = [cell.strip() for cell in lines[index+1].strip().strip('|').split('|')]
        if (not 2 <= len(header) <= 8 or len(separators) != len(header)
                or not all(re.fullmatch(r':?-{2,}:?', cell) for cell in separators)):
            continue
        names = []
        for title in header:
            symbols = re.findall(r'[A-Za-z][A-Za-z0-9_]*', title)
            name = symbols[0] if len(symbols) == 1 else re.sub(r'[\\$()]', '', title).strip()
            names.append(name)
        if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
            continue
        rows = []
        for line in lines[index+2:]:
            if not line.strip().startswith('|'):
                break
            cells = [cell.strip().replace('−', '-') for cell in line.strip().strip('|').split('|')]
            if len(cells) != len(names) or not all(re.fullmatch(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', cell) for cell in cells):
                rows = []
                break
            if not all(math.isfinite(float(cell)) for cell in cells):
                rows = []
                break
            rows.append(cells)
            if len(rows) > 1000:
                rows = []
                break
        if len(rows) >= 3:
            output = io.StringIO(newline='')
            writer = csv.writer(output, lineterminator='\n')
            writer.writerow(names)
            writer.writerows(rows)
            value = output.getvalue()
            value.encode('utf-8', errors='strict')
            return value
    return None
