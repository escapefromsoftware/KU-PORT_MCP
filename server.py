"""Ku-Port(工学院大学 学生ポータル)の MCP サーバー。

起動: .venv\\Scripts\\python.exe server.py   (stdio で MCP クライアントとつなぐ)
ブラウザ操作は kuport/portal.py、HTML の解析は kuport/parsers.py が担当する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date as Date, datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from kuport import parsers
from kuport.portal import ROOT, Portal

portal = Portal()

BulletinTab = Literal["全表示", "授業", "時間割変更", "未読", "新着", "重要", "既読", "申込", "フラグつき", "グループ"]
DAYS = "月火水木金土"
DOWNLOAD_DIR = ROOT / "downloads"
# ポータルを読むだけのツールの印(クライアントが承認の要否を判断するのに使う)
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    # サーバー終了時にブラウザも閉じる(ブラウザ自体は最初のツール呼び出しで起動する)
    try:
        yield
    finally:
        await portal.close()


mcp = MCPServer(
    "kuport",
    instructions=(
        "工学院大学の学生ポータル Ku-Port から、時間割・成績・出欠・掲示(検索・添付含む)・休講/補講・シラバス(検索含む)・空き教室を取得します。"
        "各ツールは実際にポータルを操作するので、1回あたり数秒〜十数秒かかります。"
        "掲示の本文を読む(read_bulletin)とその掲示はポータル上で既読になります。"
        "ログインが必要というエラーが出たら、その文面どおりの手順を利用者に伝えてください。"
    ),
    lifespan=lifespan,
)


@mcp.tool(annotations=READ_ONLY)
async def get_today() -> dict[str, Any]:
    """ポータルのトップ画面から、今日の授業スケジュール・終日のお知らせ・重要な掲示を取得する。"""
    return parsers.parse_top(await portal.top())


@mcp.tool(annotations=READ_ONLY)
async def get_timetable(day: str | None = None, year: int | None = None, term: str | None = None) -> dict[str, Any]:
    """時間割(学生時間割表)を取得する。省略時は今学期。

    day に「月」〜「土」を指定するとその曜日の授業だけを返す。
    year(例: 2026)と term(前期(1Q)/前期(2Q)/後期(3Q)/後期(4Q)/すべて対象)で過去・他学期も見られる。
    授業コード(code)は get_syllabus に使える。
    """
    data = parsers.parse_timetable(await portal.timetable(year, term))
    if day:
        d = day.strip()[:1]
        if d not in DAYS:
            raise ToolError("day は 月・火・水・木・金・土 のいずれかで指定してください。")
        data["classes"] = [c for c in data.get("classes", []) if c.get("day") == d]
    return data


@mcp.tool(annotations=READ_ONLY)
async def get_grades(in_progress_only: bool = False) -> dict[str, Any]:
    """成績照会画面から、科目ごとの成績・GPA(学期別/通算)・単位修得状況を取得する。

    in_progress_only=True なら履修中(未評価)の科目だけを返す。
    """
    data = parsers.parse_grades(await portal.grades())
    if in_progress_only:
        data["courses"] = [c for c in data.get("courses", []) if c.get("in_progress")]
    return data


@mcp.tool(annotations=READ_ONLY)
async def get_attendance(term: str | None = None, course: str | None = None) -> dict[str, Any]:
    """出欠状況(授業ごとの出席率と各回の出欠)を取得する。

    term は「2026年度 前期(1Q)」の形式(省略時は今学期)。course に授業名の一部か授業コードを渡すとその授業だけ返す。
    """
    data = parsers.parse_attendance(await portal.attendance(term))
    if course:
        key = course.strip()
        data["courses"] = [
            c for c in data.get("courses", []) if key in (c.get("name") or "") or key == c.get("code")
        ]
    return data


def _page_items(data: dict, unread_only: bool, offset: int, limit: int) -> None:
    # 各セクションの掲示を絞り込み・ページ分けする(件数が多いと応答が大きくなりすぎるため)
    for section in data.get("sections", []):
        items = section.get("items", [])
        if unread_only:
            items = [it for it in items if it.get("unread")]
        section["items"] = items[offset : offset + limit]
        section["returned"] = len(section["items"])
        section["has_more"] = offset + limit < len(items)


@mcp.tool(annotations=READ_ONLY)
async def list_bulletins(
    tab: BulletinTab = "グループ",
    show_all: bool = False,
    group: str | None = None,
    keyword: str | None = None,
    unread_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """掲示板の一覧を取得する(本文は含まない)。

    tab: グループ(既定。カテゴリ別で差出人・日付つき)/全表示(新しい順。件名のみ)/授業/時間割変更/
    未読/新着/重要/既読/申込/フラグつき。
    keyword を指定すると件名・差出人・本文のキーワード検索の結果になる(ポータルの検索機能)。
    通常は各タブ(グループでは各カテゴリ)の新しいものから十数件だけで、total が全件数。
    show_all=True で「すべて表示する」を押して全件を取る。グループタブでは group にカテゴリ名
    (例: 履修・成績・授業に関するお知らせ)を指定し、そのカテゴリを全件にする。
    各セクションの items は offset 件目から limit 件まで返す(returned/has_more で判断)。
    本文は read_bulletin に title と、同じ tab・group・keyword・nth を渡して取得する。
    """
    data = parsers.parse_bulletin_list(await portal.bulletins(tab, show_all, group, keyword))
    _page_items(data, unread_only, offset, limit)
    if group and tab == "グループ":
        # 指定カテゴリだけに絞る(他カテゴリは十数件のままで紛らわしいため)
        data["sections"] = [s for s in data.get("sections", []) if s.get("name") == group.strip()]
    if keyword:
        data["keyword"] = keyword
    return data


@mcp.tool()
async def read_bulletin(
    title: str,
    tab: BulletinTab = "グループ",
    nth: int = 0,
    group: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """掲示の本文と添付ファイル名を取得する。title は list_bulletins で得た件名そのまま、
    tab・group・keyword・nth も一覧取得時の値を渡す。

    最初の十数件に見つからない古い掲示は、自動で「すべて表示する」を押して探す。
    添付ファイルの中身が必要なら download_bulletin_attachments で保存してから読む。
    開いた掲示はポータル上で既読になる。
    """
    result = await portal.bulletin_detail(title, tab, nth, group=group, keyword=keyword)
    detail = parsers.parse_bulletin_detail(result["detail"])
    if detail is None:
        raise ToolError("掲示のダイアログを読み取れませんでした。")
    if result["files"]:
        detail["attachments"] = parsers.parse_attachments(result["files"]) or []
    return detail


@mcp.tool()
async def download_bulletin_attachments(
    title: str,
    tab: BulletinTab = "グループ",
    nth: int = 0,
    group: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """掲示の添付ファイルをすべて KUPORT-MCP/downloads/ に保存し、保存先のパスを返す。

    引数の意味は read_bulletin と同じ。保存した PDF などはファイル読み取り機能で中身を確認できる。
    """
    result = await portal.bulletin_detail(
        title, tab, nth, group=group, keyword=keyword, download_dir=DOWNLOAD_DIR
    )
    if not result["files"]:
        return {"title": title, "saved": [], "message": "この掲示に添付ファイルはありません。"}
    return {
        "title": title,
        "attachments": parsers.parse_attachments(result["files"]) or [],
        "saved": result["saved"],
    }


@mcp.tool(annotations=READ_ONLY)
async def get_class_changes() -> dict[str, Any]:
    """休講・補講・教室変更など、自分の授業の時間割変更の掲示一覧を取得する。"""
    data = parsers.parse_bulletin_list(await portal.bulletins("時間割変更"))
    if data.get("empty"):
        data["message"] = "現在、時間割変更(休講・補講など)の掲示はありません。"
    return data


@mcp.tool(annotations=READ_ONLY)
async def get_syllabus(course_code: str, name: str | None = None, year: int | None = None) -> dict[str, Any]:
    """授業のシラバスを取得する。course_code は get_timetable や search_syllabus の code(例: A1234567)。

    今学期の時間割になければシラバス検索で探す。その場合は name(授業名の一部)を渡すと確実に見つかる。
    year は開講年度(省略時は画面の既定)。
    """
    data = parsers.parse_syllabus(await portal.syllabus(course_code, name, year))
    if data is None:
        raise ToolError("シラバスのダイアログを読み取れませんでした。")
    return data


@mcp.tool(annotations=READ_ONLY)
async def search_syllabus(
    name: str | None = None,
    teacher: str | None = None,
    keyword: str | None = None,
    year: int | None = None,
    term: str | None = None,
) -> dict[str, Any]:
    """シラバスを検索する(履修していない授業も対象)。

    name: 授業科目名の一部 / teacher: 担当教員名の一部 / keyword: シラバス本文のキーワード。
    year(例: 2026)と term(前期(1Q)/前期(2Q)/後期(3Q)/後期(4Q)/すべて対象)。term 省略時は今学期。
    結果の code を get_syllabus に渡すと詳細が読める。
    """
    if not (name or teacher or keyword):
        raise ToolError("name・teacher・keyword のどれかを指定してください。")
    return parsers.parse_syllabus_search(await portal.syllabus_search(name, teacher, keyword, year, term))


@mcp.tool(annotations=READ_ONLY)
async def get_vacant_rooms(
    date: str | None = None,
    campus: str | None = None,
    building: str | None = None,
    floor: int | None = None,
    from_time: str | None = None,
    to_time: str = "20:00",
    min_minutes: int = 60,
    include_bookings: bool = False,
) -> dict[str, Any]:
    """空き教室を探す(教室利用状況)。授業と予約が入っていない時間帯を、教室ごとに返す。

    date: YYYY-MM-DD(省略時は今日)。
    campus: 「八王子」「新宿」など / building: 「2号館」「高層棟」など(部分一致、全角半角は区別しない) / floor: 階(地下は負の数)。
    from_time〜to_time(HH:MM)の間で、min_minutes 分以上連続して空いている時間帯がある教室だけを返す。
    from_time の省略時は、今日なら現在時刻、それ以外の日は 09:00。
    campus/building/floor を絞らないと結果が非常に多くなるので、できるだけ指定すること。
    ポータルの予約情報の隙間を計算した結果で、休講になった授業の教室は「授業あり」のままになる。
    include_bookings=True で、各教室の予約(授業名など)も返す。
    """
    try:
        day = datetime.strptime(date.strip().replace("/", "-"), "%Y-%m-%d").date() if date else Date.today()
    except ValueError:
        raise ToolError("date は YYYY-MM-DD の形式で指定してください。") from None
    if from_time is None:
        now = datetime.now()
        from_time = now.strftime("%H:%M") if day == now.date() else "09:00"
    start, end = parsers.to_minutes(from_time), parsers.to_minutes(to_time)
    if start is None or end is None or start >= end:
        raise ToolError("from_time と to_time は HH:MM の形式で、from_time < to_time になるよう指定してください。")
    if min_minutes < 1:
        raise ToolError("min_minutes は 1 以上にしてください。")
    floor_label = None if floor is None else (f"{floor}階" if floor > 0 else f"地下{-floor}階")

    html = await portal.room_usage(day.strftime("%Y/%m/%d"), campus, building, floor_label)
    rooms = []
    for room in parsers.parse_room_usage(html)["rooms"]:
        slots = parsers.free_slots(room["bookings"], start, end, min_minutes)
        if not slots:
            continue
        item = {"room": room["room"], "free": slots}
        if include_bookings:
            item["bookings"] = room["bookings"]
        rooms.append(item)
    return {
        "date": day.strftime("%Y-%m-%d"), "window": f"{parsers.format_minutes(start)}-{parsers.format_minutes(end)}",
        "min_minutes": min_minutes, "count": len(rooms), "rooms": rooms,
    }


if __name__ == "__main__":
    mcp.run()
