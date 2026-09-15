"""Локальный smoke-валидатор: проверяет синтаксис HTML/CSS и наличие JS-ID."""
import pathlib, re, sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(r"d:\ClickJurist Production")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "script.js").read_text(encoding="utf-8")

print("=== HTML size:", len(HTML), "chars ===")
print("=== CSS  size:", len(CSS),  "chars ===")
print("=== JS   size:", len(JS),   "chars ===")
print()

# 1) CSS: баланс фигурных скобок и круглых скобок
opens = CSS.count("{")
closes = CSS.count("}")
print(f"[CSS] braces  open={opens}  close={closes}  -> {'OK' if opens == closes else 'FAIL'}")
parens_open = CSS.count("(")
parens_close = CSS.count(")")
print(f"[CSS] parens  open={parens_open}  close={parens_close}  -> {'OK' if parens_open == parens_close else 'FAIL'}")
print()

# 2) HTML: проверка базовой сбалансированности основных тегов
def count_tags(html, tag):
    return len(re.findall(rf"<{tag}\b", html, flags=re.IGNORECASE)) - len(re.findall(rf"</{tag}\s*>", html, flags=re.IGNORECASE))

for tag in ["html", "head", "body", "header", "main", "section", "footer", "aside", "div", "span", "a", "svg", "g", "defs"]:
    diff = count_tags(HTML, tag)
    print(f"[HTML] <{tag}> diff(open-close)={diff} -> {'OK' if diff == 0 else 'FAIL'}")

# 3) Все ID, которые запрашивает JS, должны существовать в HTML
js_ids = set(re.findall(r'\$\("([a-zA-Z0-9_]+)"\)', JS))
js_ids |= set(re.findall(r'\$\(\$\("([a-zA-Z0-9_]+)"\)\)', JS))
js_ids |= set(re.findall(r"getElementById\(\"([a-zA-Z0-9_]+)\"\)", JS))
# Уникальный список
html_ids = set(re.findall(r'id="([a-zA-Z0-9_]+)"', HTML))

missing = js_ids - html_ids - {"this"}  # 'this' не ID
print()
print(f"[JS]  Уникальных запрашиваемых ID: {len(js_ids)}")
print(f"[HTML] Уникальных ID в разметке: {len(html_ids)}")
print(f"[CHECK] Отсутствуют в HTML: {sorted(missing) if missing else 'нет — всё на месте'}")
print()

# 4) Стилистические проверки: никаких жёлтых оттенков
yellow_terms = ["ffd166", "ffb627", "255,209,102", "yellow", "ffcc00", "ffd800", "ffaa00"]
print("[CHECK] Жёлтые цвета:")
for term in yellow_terms:
    count_css = CSS.lower().count(term.lower())
    count_html = HTML.lower().count(term.lower())
    if count_css or count_html:
        print(f"  ⚠ '{term}': CSS={count_css}, HTML={count_html}")
    else:
        print(f"  ✓ '{term}' не найден")

# 5) Проверка критических элементов бренда
print()
brand_checks = [
    ('class="brand-shield"', "Inline SVG-щит логотипа"),
    ('class="brand-line"', "Текстовая строка CLICKJURIST"),
    ('class="brand-highlight"', "Подсветка JURIST"),
    ('class="brand-tagline"', "Тэглайн"),
    ('class="topbar"', "Sticky-шапка"),
    ('id="sessionChip"', "Чип сессии"),
    ('id="paywall"', "Paywall (модалка оплаты)"),
    ('id="runQueryBtn"', "Кнопка консультации"),
]
print("[CHECK] Структурные элементы:")
for needle, desc in brand_checks:
    status = "✓" if needle in HTML else "✗"
    print(f"  {status} {desc} ({needle})")
