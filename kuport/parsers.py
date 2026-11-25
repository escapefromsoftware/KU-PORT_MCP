"""Ku-Port の取得済み HTML を解析する、副作用のない関数群。"""

import re
from collections import Counter

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _text(node: Tag | None) -> str:
    return _clean(node.get_text(" ", strip=True)) if node else ""


def _number(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match[0]) if match else None


def _visible(node: Tag) -> bool:
    # 自動採番 ID を使わず、親要素も含めた非表示指定を確認する。
    for el in [node, *node.parents]:
        if not isinstance(el, Tag):
            continue
        if el.get("aria-hidden") == "true" or el.has_attr("hidden"):
            return False
        if set(el.get("class", [])) & {"ui-helper-hidden", "hiddenStyle", "dispNone"}:
            return False
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", el.get("style", ""), re.I):
            return False
    return True


def _plain(node: Tag | None) -> str:
    """インライン要素をつなぎ、段落・改行・表の区切りを保持する。"""
    blocks = {
        "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "blockquote", "pre",
    }

    def walk(el) -> str:
        if isinstance(el, Comment):
            return ""
        if isinstance(el, NavigableString):
            return re.sub(r"\s+", " ", str(el))
        if not isinstance(el, Tag) or el.name in {"script", "style"}:
            return ""
        if not _visible(el):
            return ""
        if el.name == "br":
            return "\n"
        if el.name == "table":
            rows = []
            for row in el.find_all("tr"):
                if row.find_parent("table") is el:
                    rows.append("\t".join(
                        _clean(walk(cell))
                        for cell in row.find_all(["th", "td"], recursive=False)
                    ))
            return "\n" + "\n".join(rows) + "\n"
        value = "".join(walk(child) for child in el.children)
        return "\n" + value + "\n" if el.name in blocks else value

    if node is None:
        return ""
    value = "\n".join(line.strip() for line in walk(node).splitlines())
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _teachers(node: Tag | None) -> list[str]:
    return [name for part in re.split(r"[、，,／/\n]+", _plain(node)) if (name := _clean(part))]


def _tab_panel(soup: Tag, tab: Tag) -> Tag | None:
    # href の完全一致で関連付ける。ID 内の自動採番部分の値には依存しない。
    link = tab.find("a", href=True)
    target = tab.get("aria-controls") or (
        link["href"][1:] if link and link["href"].startswith("#") else None
    )
    return soup.find(id=target) if target else None


def detect_screen(html: str) -> str | None:
    soup = _soup(html)
    match = re.search(r"\[([A-Z][a-z]{2}\d{3})\]", (soup.body or soup).get_text(" ", strip=True))
    if match:
        return match[1]
    # 共通メニューにも「ポータルトップ」があるので、それだけでは判定しない。
    if soup.select_one(".dateDisp") and soup.select_one(".ourscheduleArea, .lessonArea"):
        return "Xuk004"
    return None


def parse_top(html: str) -> dict:
    soup = _soup(html)
    result = {"important_notices": [], "date": None, "day_notices": [], "schedule": []}
    for tab in soup.select(".ui-tabs-nav > [role=tab]"):
        if _text(tab) != "重要":
            continue
        panel = _tab_panel(soup, tab)
        if panel:
            for row in panel.select(".ui-datalist-item"):
                title = _text(row.select_one(".textTitle"))
                if title:
                    sender = " ".join(
                        _text(el) for el in row.select(".textFrom")
                        if _text(el) not in {"[", "]"}
                    )
                    result["important_notices"].append({
                        "date": _text(row.select_one(".textDate")) or None,
                        "title": title,
                        "sender": sender or None,
                        "new": row.select_one(".newInfo") is not None,
                    })
        break
    date = next((el for el in soup.select(".dateDisp") if _visible(el)), None)
    result["date"] = _text(date) or None
    for heading in soup.select("h3"):
        if _text(heading) == "終日" and _visible(heading):
            result["day_notices"] = [
                _text(a) for a in heading.parent.select(".ourscheduleArea a")
                if _text(a) and _visible(a)
            ]
            break
    for lesson in soup.select(".lessonArea"):
        if not _visible(lesson):
            continue
        name = _text(lesson.select_one(".lessonTitle"))
        if not name:
            continue
        detail = lesson.select_one(".lessonDetail")
        result["schedule"].append({
            "time": _text(lesson.select_one(".lessonHead p")) or None,
            "name": name,
            "teachers": _teachers(detail.find("p") if detail else None),
            "room": _text(detail.find("div", recursive=False) if detail else None) or None,
        })
    return result


def _grid(table: Tag) -> list[list[Tag | None]]:
    """空セルと結合セルを含む表を、見出しと同じ列位置に展開する。"""
    rows = []
    spans = {}
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        occupied = {col: cell for col, (cell, _) in spans.items()}
        future = {col: (cell, left - 1) for col, (cell, left) in spans.items() if left > 1}
        col = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while col in occupied:
                col += 1
            try:
                width = max(1, int(cell.get("colspan", 1)))
                height = max(1, int(cell.get("rowspan", 1)))
            except ValueError:
                width = height = 1
            for offset in range(width):
                occupied[col + offset] = cell
                if height > 1:
                    future[col + offset] = (cell, height - 1)
            col += width
        rows.append([occupied.get(i) for i in range(max(occupied, default=-1) + 1)])
        spans = future
    return rows


def parse_timetable(html: str) -> dict:
    soup = _soup(html)
    result = {"term": None, "total_credits": None, "classes": []}
    for table in soup.select("table.classTable"):
        fieldset = table.find_parent("fieldset")
        if fieldset:
            result["term"] = result["term"] or _text(fieldset.find("legend")) or None
        total = next((
            el for el in table.parent.select(".alignRight")
            if "合計単位" in _text(el)
        ), None)
        if total:
            value = re.search(r"合計単位\s*([\d.,]+)", _text(total))
            if value:
                amount = _number(value[1])
                if amount is not None:
                    result["total_credits"] = (result["total_credits"] or 0.0) + amount
        days = {}
        period_col = None
        for row in _grid(table):
            headers = {
                i: _text(cell)[0] for i, cell in enumerate(row)
                if cell and re.fullmatch(r"[月火水木金土](?:曜日)?", _text(cell))
            }
            if headers:
                days = headers
                period_col = next((i for i in range(len(row)) if i not in days), None)
                continue
            period_cell = row[period_col] if period_col is not None and period_col < len(row) else None
            period_match = re.fullmatch(r"(\d+)\s*(?:時限|限)?", _text(period_cell))
            if not period_match:
                continue
            for col, day in days.items():
                cell = row[col] if col < len(row) else None
                if cell is None:
                    continue
                for info in cell.select("div.jugyo-info"):
                    name_node = info.select_one(".fontB")
                    if not _text(name_node):
                        continue
                    parts = info.find_all("div", recursive=False)
                    location = next((part for part in parts if part.find("span", recursive=False)), None)
                    teacher_parts = []
                    for part in name_node.find_next_siblings("div"):
                        if part is location:
                            break
                        if part.select_one("button") or set(part.get("class", [])) & {"sign", "taniSu"}:
                            continue
                        if not re.fullmatch(r"[A-Z]\d{7}", _text(part)):
                            teacher_parts.extend(_teachers(part))
                    places = location.find_all("span", recursive=False) if location else []
                    code_node = next((
                        part for part in parts
                        if re.fullmatch(r"[A-Z]\d{7}", _text(part))
                    ), None)
                    room = _text(places[0]) if places else ""
                    result["classes"].append({
                        "day": day,
                        "period": int(period_match[1]),
                        "name": _text(name_node),
                        "teachers": teacher_parts,
                        "room": room if room and room != "." else None,
                        "campus": (_text(places[1]) or None) if len(places) > 1 else None,
                        "code": _text(code_node) or None,
                        "credits": _number(_text(info.select_one(".taniSu"))),
                        "note": _text(info.select_one(".sign")) or None,
                    })
    return result


def _depth(node: Tag | None) -> int | None:
    for cls in node.get("class", []) if node else []:
        match = re.fullmatch(r"kamokuLevel(\d+)", cls)
        if match:
            return int(match[1])
    return None


def parse_grades(html: str) -> dict:
    soup = _soup(html)
    result = {
        "courses": [], "gpa_by_term": [], "gpa_total": None,
        "gpa_by_year": [], "credit_summary": [],
    }
    for table in soup.find_all("table"):
        # 外側のレイアウト表と内側のデータ表を重複して処理しない。
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        headers = [_text(cell) for row in rows for cell in row.find_all("th", recursive=False)]
        if "科目" in headers and "評価" in headers:
            path = {}
            for row in rows:
                cell = row.select_one("td.colKmkName")
                if cell is None:
                    continue
                name = _text(cell)
                level = _depth(cell)
                credits = _number(_text(row.select_one(".colTani")))
                year = _number(_text(row.select_one(".colNendo")))
                is_course = (
                    credits is not None or year is not None or level == 7
                    or any(_text(row.select_one(selector)) for selector in (
                        ".colHyoka", ".colGakki", ".colKyuinName",
                    ))
                )
                if not is_course:
                    if name and level is not None:
                        path = {depth: text for depth, text in path.items() if depth < level}
                        path[level] = name
                    continue
                # 第Ⅰ群と総合教育科目は同じ見出しセルに含まれる。
                category = " > ".join(
                    re.sub(r"^(第\S+群)\s+", r"\1 > ", path[depth])
                    for depth in sorted(path)
                )
                in_progress = (
                    "※" in _text(row.select_one(".colRishu")) or name.startswith("※")
                )
                result["courses"].append({
                    "category": category or None,
                    "name": name.lstrip("※").strip(),
                    "credits": credits,
                    "grade": _text(row.select_one(".colHyoka")) or None,
                    "year": int(year) if year is not None else None,
                    "term": _text(row.select_one(".colGakki")) or None,
                    "teacher": _text(row.select_one(".colKyuinName")) or None,
                    "in_progress": in_progress,
                })
        elif "GPA" in headers:
            for row in rows:
                cells = row.find_all("td", recursive=False)
                if len(cells) < 2:
                    continue
                label, value = _text(cells[0]), _number(_text(cells[1]))
                if label == "通算":
                    result["gpa_total"] = value
                elif re.fullmatch(r"\d{4}年度", label):
                    result["gpa_by_year"].append({"year": label, "gpa": value})
                elif label:
                    result["gpa_by_term"].append({"term": label, "gpa": value})
        elif "修得済単位" in headers:
            for row in rows:
                cells = row.find_all("td", recursive=False)
                if not cells:
                    continue
                values = {
                    label: _number(_text(cells[i])) if i < len(cells) else None
                    for i, label in enumerate(["earned", "in_progress", "total"], start=1)
                }
                item = {"category": _text(cells[0]) or None, **values}
                level = _depth(cells[0])
                if level is not None:
                    # CSS の階層番号をそのまま保持する（1、3、5など）。
                    item["depth"] = level
                result["credit_summary"].append(item)
    return result


def parse_bulletin_list(html: str) -> dict:
    soup = _soup(html)
    result = {"tab": None, "empty": False, "sections": []}
    panel = None
    for nav in soup.select(".ui-tabs-nav"):
        tabs = nav.select('[role="tab"]')
        if not any(_text(tab) == "全表示" for tab in tabs):
            continue
        tab = next((
            tab for tab in tabs
            if tab.get("aria-selected") == "true"
            or "ui-state-active" in tab.get("class", [])
        ), None)
        if tab:
            result["tab"] = _text(tab) or None
            panel = _tab_panel(soup, tab)
            if panel is None:
                widget = nav.find_parent(class_="ui-tabs")
                panel = widget.select_one('.ui-tabs-panel[aria-hidden="false"]') if widget else None
        break
    if panel is None:
        return result
    if panel.get("aria-hidden") == "true":
        return result
    result["empty"] = any(
        "出力対象のデータが存在しません。" in str(text) and _visible(text.parent)
        for text in panel.find_all(string=True)
    )
    if result["empty"]:
        return result
    sections = panel.select(".ui-accordion-content") or [panel]
    counts = Counter()
    for section in sections:
        heading = section.find_previous_sibling(["h2", "h3", "h4"])
        total_match = re.search(r"全\s*([\d,]+)\s*件", _text(section.select_one(".keijiKensu")))
        entry = {
            "name": (_text(heading) or None) if section is not panel else None,
            "total": int(total_match[1].replace(",", "")) if total_match else None,
            "items": [],
        }
        for item in section.select("dl.keiji"):
            link = item.find("a")
            title = _text(link)
            if not title:
                continue
            row = item.parent
            # fontBold は未読表示。操作ボタン「既読にする」も未読の根拠。
            unread = "fontBold" in link.get("class", []) or any(
                _text(el) == "既読にする" for el in row.select(".ui-button-text")
            )
            sender = " ".join(
                _text(el) for el in item.select(".textFrom")
                if _text(el) not in {"[", "]"}
            ).strip("[] ")
            # グループ表示の差出人・日付は、リンク直後の裸のテキストにも入る。
            trailing = _clean(" ".join(
                el.get_text(" ", strip=True) if isinstance(el, Tag) else str(el)
                for el in link.next_siblings
            ))
            sender_match = re.search(r"^\[([^\]]+)\]", trailing)
            if not sender and sender_match:
                sender = _clean(sender_match[1])
            date_match = re.search(
                r"\d{4}/\d{2}/\d{2}", _text(item.select_one(".textDate")) or trailing
            )
            # アコーディオンの開閉によらず、アイコン自体の表示指定を見る。
            important = any(
                not (set(icon.get("class", [])) & {"hiddenStyle", "dispNone", "ui-helper-hidden"})
                and not icon.has_attr("hidden")
                and icon.get("aria-hidden") != "true"
                and not re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", icon.get("style", ""), re.I)
                for icon in item.select(".fa-exclamation-circle, .fa-exclamation-triangle")
            )
            entry["items"].append({
                "title": title,
                "sender": sender or None,
                "date": date_match[0] if date_match else None,
                "unread": unread,
                "important": important,
                "nth": counts[title],
            })
            counts[title] += 1
        result["sections"].append(entry)
    # 実画面では「データが存在しません」の表示を検出できないことがあるため、1件もなければ空扱いにする
    if not any(section["items"] for section in result["sections"]):
        result["empty"] = True
        result["sections"] = []
    return result


def _label_rows(table: Tag):
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            yield _text(cells[0]).rstrip("：:"), cells[1]


def parse_bulletin_detail(html: str) -> dict | None:
    soup = _soup(html)
    dialog = soup.find(id="bsd00702:dialog", attrs={"aria-hidden": "false"})
    if dialog is None:
        return None
    result = {
        "sender": None, "category": None, "title": None, "body": "",
        "period_start": None, "period_end": None, "attachments": [], "extra": {},
    }
    keys = {"差出人": "sender", "カテゴリ": "category", "件名": "title"}
    for table in dialog.select("table.singleTable"):
        for label, cell in _label_rows(table):
            value = _plain(cell)
            if label in keys:
                result[keys[label]] = _text(cell) or None
            elif label == "本文":
                body = cell.select_one(".fr-view")
                result["body"] = _plain(body if body is not None else cell)
            elif label == "掲示期間":
                dates = re.findall(
                    r"\d{4}/\d{2}/\d{2}(?:[（(][^）)]*[）)])?(?:\s+\d{1,2}:\d{2})?",
                    value,
                )
                result["period_start"] = dates[0] if dates else None
                result["period_end"] = dates[1] if len(dates) > 1 else None
            elif "添付" in label:
                names = [_text(el) for el in cell.select("a, .ui-button-text") if _text(el)]
                if not names:
                    names = [line for line in value.splitlines() if line]
                result["attachments"].extend({"name": name} for name in names)
            elif label:
                result["extra"][label] = value
    return result


def parse_syllabus(html: str) -> dict | None:
    soup = _soup(html)
    dialog = next((
        el for el in soup.select('[role="dialog"][aria-hidden="false"]')
        if _text(el.select_one(".ui-dialog-title")) == "シラバス照会"
    ), None)
    if dialog is None:
        return None
    fields = {}
    for row in dialog.select(".rowStyle"):
        cells = row.find_all("div", recursive=False)
        for i, cell in enumerate(cells):
            if "ui-widget-header" not in cell.get("class", []):
                continue
            label = _text(cell)
            if not label:
                continue
            following = []
            for sibling in cells[i + 1:]:
                if "ui-widget-header" in sibling.get("class", []):
                    break
                following.append(sibling)
            # 第N回の見出しには、同じ行の「授業形態」も含めて残す。
            if not following and re.fullmatch(r"第\d+回", label):
                following = cells[i + 1:]
            value = "\n".join(text for el in following if (text := _plain(el)))
            # 授業計画の同名ラベルを上書きせず、出現順に連結する。
            fields[label] = "\n\n".join(part for part in [fields.get(label, ""), value] if part)
    return {"title": fields.get("科目名") or None, "fields": fields}


def _course_label(node: Tag | None) -> tuple[str | None, str]:
    """科目コードを分離し、省略表示より長い title 属性があれば採用する。"""
    text = _text(node)
    titles = [
        _clean(el.get("title", ""))
        for el in [node, *node.select("[title]")] if el is not None
    ] if node is not None else []
    pattern = r"^([A-Z]\d{7})(?!\d)\s*"
    code = re.match(pattern, text)
    candidates = [text, *filter(None, titles)]
    full = max(candidates, key=lambda value: len(re.sub(pattern, "", value)))
    code = code or re.match(pattern, full)
    return (code[1] if code else None), re.sub(pattern, "", full).strip()


def _headed_tables(soup: Tag, required: set[str]):
    """表示中の表を見出しで識別し、固定見出し用の複製表を除外する。"""
    for table in soup.find_all("table"):
        if not _visible(table):
            continue
        rows = _grid(table)
        for index, row in enumerate(rows):
            headers = {_text(cell): col for col, cell in enumerate(row) if cell and cell.name == "th"}
            if required <= headers.keys():
                yield table, headers, rows[index + 1:]
                break


def _attendance_mark(cell: Tag, legend: dict) -> str | None:
    mark = _text(cell.select_one(".syuketsuKbnMark")) or _text(cell.select_one(".jugyoList"))
    if mark:
        return mark
    # 文字がない場合は代替テキストを優先し、実画面で確認できたクラスも使う。
    for el in [cell, *cell.select("[title], [alt], .attendance, .absence")]:
        for attr in ("title", "alt"):
            value = _clean(el.get(attr, ""))
            if value in legend:
                return value
            for symbol, status in legend.items():
                if value == status:
                    return symbol
        for cls, symbol in {"attendance": "〇", "absence": "×"}.items():
            if cls in el.get("class", []):
                return symbol
    return None


def parse_attendance(html: str) -> dict:
    soup = _soup(html)
    result = {"term": None, "base_date": None, "legend": {}, "courses": []}
    selected = soup.select_one('select[data-p-label="開講年度学期"] option[selected]')
    result["term"] = _text(selected) or None
    date = soup.select_one('input[data-p-label="出席率基準日"]')
    match = re.fullmatch(r"\d{4}/\d{2}/\d{2}", date.get("value", "").strip()) if date else None
    result["base_date"] = match[0] if match else None
    for area in soup.select(".alignRight"):
        if _visible(area):
            result["legend"].update(re.findall(r"([〇早遅×＊－])\s*[：:]\s*([^\s：:]+)", _text(area)))
    for _, headers, rows in _headed_tables(soup, {"曜日時限", "授業科目", "出席率"}):
        for row in rows:
            def cell(label):
                col = headers.get(label)
                return row[col] if col is not None and col < len(row) else None

            subject = cell("授業科目")
            if subject is None or subject.name != "td" or not _text(subject):
                continue
            code, name = _course_label(subject)
            teacher = None
            # 閉じ括弧が省略された教員名も、開始括弧以降の取れる範囲を残す。
            suffix = re.search(r"[（(]([^（）()]*)[）)]$", name)
            if suffix is None and re.search(r"(?:…|\.\.\.)$", name):
                suffix = re.search(r"[（(]([^（）()]*)$", name)
            if suffix:
                teacher = re.sub(r"(?:…|\.\.\.)+$", "", suffix[1]).strip() or None
                name = name[:suffix.start()].strip()
            sessions = []
            for label, col in headers.items():
                number = re.fullmatch(r"(\d+)回", label)
                if not number or col >= len(row) or row[col] is None:
                    continue
                date_text = _text(row[col].select_one(".jugyoDate"))
                if not re.fullmatch(r"\d{2}/\d{2}", date_text):
                    continue
                mark = _attendance_mark(row[col], result["legend"])
                sessions.append({
                    "no": int(number[1]), "date": date_text, "mark": mark,
                    "status": result["legend"].get(mark),
                })
            result["courses"].append({
                "slot": _text(cell("曜日時限")) or None, "code": code,
                "name": name or None, "teacher": teacher,
                "rate": _number(_text(cell("出席率"))), "sessions": sessions,
                "summary": dict(Counter(s["status"] for s in sessions if s["status"] is not None)),
            })
    return result


def parse_syllabus_search(html: str) -> dict:
    soup = _soup(html)
    result = {"total": None, "results": []}
    required = {"曜日時限", "授業科目", "担当教員", "開講区分", "開講年度学期", "単位数"}
    for table, headers, rows in _headed_tables(soup, required):
        area = table.find_parent(class_="ui-datatable")
        count = (area or table).select_one(".ui-paginator-current")
        match = re.search(r"([\d,]+)\s*件", _text(count))
        result["total"] = int(match[1].replace(",", "")) if match else None
        for row in rows:
            # 0件時の colspan 付きメッセージ行は検索結果として扱わない。
            if len(row) <= max(headers.values()) or len({id(c) for c in row if c}) < len(required):
                continue
            def cell(label):
                return row[headers[label]]

            subject = cell("授業科目")
            if subject is None or subject.name != "td" or not _text(subject):
                continue
            code, name = _course_label(subject)
            result["results"].append({
                "slot": _text(cell("曜日時限")) or None, "code": code, "name": name or None,
                "teachers": _teachers(cell("担当教員")),
                "kubun": _text(cell("開講区分")) or None,
                "term": _text(cell("開講年度学期")) or None,
                "credits": _number(_text(cell("単位数"))),
            })
    return result


def parse_attachments(html: str) -> list[dict] | None:
    soup = _soup(html)
    dialog = soup.find(id="pkx02201:dialog", attrs={"aria-hidden": "false"})
    if dialog is None:
        return None
    result = []
    for index, row in enumerate(dialog.select(".tableDownloadRow")):
        value = _text(row.select_one(".downLoadCellFilNm"))
        # ファイル名自身の括弧を残し、末尾の容量表示だけを分離する。
        size = re.search(r"\s*[（(]\s*([\d.,]+\s*(?:[KMGT]?B|バイト))\s*[）)]$", value, re.I)
        result.append({
            "index": index, "name": (value[:size.start()].strip() if size else value) or None,
            "size": _clean(size[1]) if size else None,
        })
    return result
