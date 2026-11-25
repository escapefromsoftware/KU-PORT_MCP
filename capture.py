"""画面キャプチャ用ツール。

ブラウザを表示した状態でポータルを開くので、手動でログイン（必要ならワンタイムパスワードも）してから
お知らせ・時間割・成績などの画面を順に開き、ターミナルで Enter を押すとその画面の HTML とスクショを
dumps/ に保存する。保存した HTML をもとに MCP サーバーの解析処理を書く。

Cookie は profile/ に残るので、ここでログインしておけば後で MCP サーバーがそのセッションを使い回せる。
"""

import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent
# portal.py と同じく KUPORT_PROFILE_DIR でプロファイルを切り替えられる(例: profile-desktop)
PROFILE_DIR = ROOT / (os.getenv("KUPORT_PROFILE_DIR") or "profile")
DUMP_DIR = ROOT / "dumps"


def safe_name(text: str) -> str:
    # ファイル名に使えない文字を置き換え、長すぎるタイトルは切り詰める
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")[:40] or "untitled"


def page_label(html: str, url: str) -> str:
    # JSF は URL が1つ前の画面のままになることがあるので、本文中の画面コード（例: [Bsd007]）を優先する
    m = re.search(r"\[([A-Z][a-z]{2}\d{3})\]", html)
    if m:
        return m.group(1)
    # 画面コードがない画面（トップなど）は URL 末尾を使う
    return safe_name(url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].removesuffix(".xhtml"))


def save_page(page: Page, index: int) -> None:
    # 画面遷移の途中だと content() が失敗するので、通信が落ち着くまで待つ
    # （定期通信がある画面だと networkidle にならないので、タイムアウトしても続行する）
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    stamp = datetime.now().strftime("%H%M%S")
    html = page.content()
    base = DUMP_DIR / f"{index:02d}_{stamp}_{page_label(html, page.url)}"
    base.with_suffix(".html").write_text(html, encoding="utf-8")
    page.screenshot(path=str(base.with_suffix(".png")), full_page=True)

    # UNIPA 系は frame を使う画面があるので、子フレームの中身も別ファイルで保存する
    for i, frame in enumerate(page.frames[1:], start=1):
        try:
            Path(f"{base}_frame{i}.html").write_text(frame.content(), encoding="utf-8")
        except Exception as e:  # 読み込み途中のフレームなどは無視
            print(f"  フレーム{i}は保存できませんでした: {e}")

    # 遷移の仕組みを調べるために URL も記録しておく
    with (DUMP_DIR / "urls.txt").open("a", encoding="utf-8") as f:
        f.write(f"{base.name}\t{page.url}\n")
    print(f"  保存しました: {base.name}.html / .png")


def main() -> None:
    load_dotenv(ROOT / ".env")
    url = os.getenv("PORTAL_URL")
    if not url:
        sys.exit(".env に PORTAL_URL を設定してください（.env.example を参照）")

    DUMP_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        # Windows に標準で入っている Edge を使う（Chromium の追加ダウンロードが不要）
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="msedge", headless=False, viewport={"width": 1280, "height": 900}
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)

        print("ブラウザでログインし、保存したい画面を開いてから Enter を押してください。")
        print("q + Enter で終了します（Cookie は profile/ に保存されます）。")
        index = 1
        while True:
            try:
                cmd = input(f"[{index}] Enter=保存 / q=終了 > ").strip().lower()
            except EOFError:  # 入力が閉じられた場合も正常終了扱いにする
                break
            if cmd == "q":
                break
            # 新しいタブで開く画面もあるので、まだ開いている最後のタブを対象にする
            open_pages = [pg for pg in context.pages if not pg.is_closed()]
            if not open_pages:
                print("  ブラウザが閉じられたので終了します。")
                break
            # 遷移中などで失敗しても終了せず、少し待って最大3回までやり直す
            for attempt in range(1, 4):
                try:
                    save_page(open_pages[-1], index)
                    index += 1
                    break
                except Exception:
                    print(f"  保存に失敗しました（{attempt}/3回目）:")
                    traceback.print_exc()
                    open_pages[-1].wait_for_timeout(1500)
            else:
                print("  保存できませんでした。画面の表示が終わってからもう一度 Enter を押してください。")
        try:
            context.close()
        except Exception:
            pass  # 先にブラウザが閉じられていた場合


if __name__ == "__main__":
    main()
