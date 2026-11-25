"""Ku-Port をブラウザ(Playwright + Edge)で操作して、各画面の HTML を取ってくる層。

- 解析はしない(kuport/parsers.py の担当)。ここは「目的の画面を表示して HTML を返す」ことだけに責任を持つ。
- Ku-Port は JSF なので URL 直打ちでの遷移はできない。毎回トップを開き、メニューの文字を頼りに遷移する。
- 要素 ID は j_idtNNN の自動採番で変わるので、画面上の文字列・title 属性・意味のある固定 ID で要素を探す。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
# ToolError を継承すると、MCP SDK がメッセージをそのままクライアント(Claude)に返す。
# 普通の例外だと「Error executing tool」だけになり、手動ログインの案内などが伝わらない
from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

ROOT = Path(__file__).resolve().parent.parent
# ブラウザのプロファイル(Cookie)の置き場所。同じプロファイルは同時に1つのブラウザしか開けないので、
# Claude Code とデスクトップアプリのように複数のクライアントから使うときは KUPORT_PROFILE_DIR で分ける
PROFILE_DIR = ROOT / (os.getenv("KUPORT_PROFILE_DIR") or "profile")

# 画面遷移や Ajax の待ち時間(ミリ秒)
NAV_TIMEOUT_MS = 30_000
SCREEN_CODE_RE = re.compile(r"\[([A-Z][a-z]{2}\d{3})\]")


class PortalError(ToolError):
    """ポータル操作の失敗。メッセージはそのまま Claude に返すので、利用者がやるべきことを書く。"""


class LoginRequired(PortalError):
    """自動ログインできず、手動ログイン(ワンタイムパスワード等)が必要な状態。"""


class SessionLost(PortalError):
    """操作中にポータル側でセッションが切られた(別の場所でのログインや時間切れ)。ログインし直して再試行する。"""


# セッションが切られたときにポータルが出す画面の文言
LOGGED_OUT_TEXT = "自動的にログアウトされました"
SHOW_ALL_TITLE = "このグループに配信されているすべての掲示を表示します。"
ATTACH_TITLE = "登録されている添付ファイルを表示します。"


def _ps_quote(value: str) -> str:
    # PowerShell の単一引用符文字列にする(中の ' は '' に重ねる)
    return "'" + value.replace("'", "''") + "'"


def manual_login_instructions(root: Path = ROOT, profile_dir: Path = PROFILE_DIR) -> str:
    """手動ログインの案内文。PowerShell にそのまま貼り付けられるコマンドを含める。

    システム側の Python では Playwright が見つからないので、必ずこのプロジェクトの .venv の Python を使わせる。
    プロファイルは MCP が実際に使っているもの(PROFILE_DIR)を絶対パスで指定する。
    """
    commands = "\n".join([
        f"Set-Location -LiteralPath {_ps_quote(str(root))}",
        f"$env:KUPORT_PROFILE_DIR = {_ps_quote(str(profile_dir))}",
        r".\.venv\Scripts\python.exe capture.py",
    ])
    return (
        "手動ログインが必要です。次の3行を PowerShell に貼り付けて実行し、開いた Edge でログイン"
        "(ワンタイムパスワード含む)してから、ターミナルで q を入力して Enter を押してください。"
        "その後、もう一度ツールを呼んでください。\n" + commands
    )


class Portal:
    def __init__(self) -> None:
        load_dotenv(ROOT / ".env")
        self.url = os.getenv("PORTAL_URL") or ""
        self.user_id = os.getenv("KUPORT_ID") or ""
        self.password = os.getenv("KUPORT_PASSWORD") or ""
        # デバッグ時は KUPORT_HEADLESS=0 にするとブラウザが見える
        self.headless = os.getenv("KUPORT_HEADLESS", "1") != "0"
        self._lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------ 起動・終了

    async def _start(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page
        if not self.url:
            raise PortalError("KUPORT-MCP/.env に PORTAL_URL が設定されていません。")
        if self._pw is None:
            self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="msedge",
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
        except Exception as e:
            # 同じプロファイルを capture.py 等が開いているとロックで起動できない
            raise PortalError(
                "ブラウザを起動できませんでした。capture.py など profile/ を使うブラウザが開いていたら閉じてください。"
                f"\n詳細: {e}"
            ) from e
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT_MS)
        return self._page

    async def close(self) -> None:
        """Edge と Playwright を閉じ、保持している参照を空にする(途中で失敗しても参照は必ず空にする)。"""
        try:
            if self._ctx:
                try:
                    await self._ctx.close()
                except Exception:
                    pass
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
        finally:
            self._pw = self._ctx = self._page = None

    # ------------------------------------------------------------------ 共通ヘルパー

    @staticmethod
    async def _settle(page: Page, timeout: int = 10_000) -> None:
        # 通信が落ち着くまで待つ。定期通信でネットワークが静かにならない画面もあるのでタイムアウトは無視
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    @staticmethod
    async def _content(page: Page) -> str:
        # 遷移の途中だと content() が失敗するので、少し待ってやり直す
        for _ in range(20):
            try:
                return await page.content()
            except Exception:
                await asyncio.sleep(0.5)
        return await page.content()

    async def _is_portal(self, page: Page) -> bool:
        # ログイン済みの Ku-Port 画面には上部メニュー(menuForm:mainMenu)がある
        if "ku-port" not in page.url:
            return False
        try:
            return await page.locator("[id='menuForm:mainMenu']").count() > 0
        except Exception:
            return False

    async def _wait_screen(self, page: Page, code: str, timeout_ms: int = NAV_TIMEOUT_MS) -> str:
        """本文に画面コード [code] が出るまで待って HTML を返す。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            html = await self._content(page)
            if f"[{code}]" in html:
                await self._settle(page, 5_000)
                return await self._content(page)
            if LOGGED_OUT_TEXT in html:
                raise SessionLost("ポータルから自動ログアウトされました。")
            await asyncio.sleep(0.5)
        found = SCREEN_CODE_RE.search(await self._content(page))
        raise PortalError(f"画面 {code} に遷移できませんでした(現在: {found.group(1) if found else page.url})。")

    # ------------------------------------------------------------------ ログイン

    async def _goto_top(self) -> Page:
        """トップ画面を開く。セッションが切れていれば自動ログインする。"""
        page = await self._start()
        await page.goto(self.url, wait_until="domcontentloaded")
        await self._settle(page)
        if await self._is_portal(page):
            return page
        await self._login(page)
        return page

    async def _login(self, page: Page) -> None:
        """統合認証(SECIOSS)でユーザー名 → パスワードを入力する。ワンタイムパスワードが出たら手動に回す。"""
        manual = manual_login_instructions()
        if not (self.user_id and self.password):
            raise LoginRequired(
                "ログインが切れています。.env に KUPORT_ID と KUPORT_PASSWORD を設定すると自動ログインできます。"
                "設定しない場合は" + manual
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        user_done = pass_done = False
        while loop.time() < deadline:
            if await self._is_portal(page):
                return
            # ワンタイムパスワード/多要素認証の画面(URL が otplogin.cgi になる)
            if "otplogin" in page.url:
                raise LoginRequired("ワンタイムパスワード(多要素認証)が必要です。" + manual)
            try:
                # ワンタイムパスワードの画面(送信ボタンが表示される、または入力欄がある)
                otp_btn = page.locator("#otp_send_button:visible, #otp_resend_button:visible")
                otp_input = page.locator("input[name*='otp' i]:visible, input[id*='otp' i]:visible")
                if await otp_btn.count() or await otp_input.count():
                    raise LoginRequired("ワンタイムパスワードが必要です。" + manual)

                user = page.locator("#username_input:visible")
                pw = page.locator("input[type='password']:visible")
                if not user_done and await user.count():
                    await user.fill(self.user_id)
                    # 「次へ」ボタンは入力欄の change イベントで有効になる作りなので、明示的に発火させる
                    await user.dispatch_event("change")
                    await page.locator("#login_button:enabled").click()
                    user_done = True
                elif not pass_done and await pw.count():
                    await pw.first.fill(self.password)
                    await pw.first.dispatch_event("change")
                    await pw.first.press("Enter")
                    pass_done = True
                elif pass_done and "secioss" in page.url:
                    # パスワード送信後も認証画面のまま → エラー表示があれば失敗とみなす
                    body = await page.locator("body").inner_text()
                    if re.search(r"(正しくありません|失敗|エラー|invalid)", body, re.I):
                        raise LoginRequired("自動ログインに失敗しました(ID/パスワードを確認してください)。" + manual)
            except LoginRequired:
                raise
            except Exception:
                pass  # 画面遷移中の一時的なエラーは無視して次のループで再確認
            await asyncio.sleep(0.7)
        raise LoginRequired(f"自動ログインがタイムアウトしました(現在: {page.url})。" + manual)

    # ------------------------------------------------------------------ 画面遷移

    async def _open_menu(self, label: str, code: str) -> Page:
        """トップから上部メニューの項目(表示文字で指定)を開き、画面 code になるまで待つ。"""
        page = await self._goto_top()
        link = page.locator("a.ui-menuitem-link").filter(
            has=page.locator("span.ui-menuitem-text", has_text=re.compile(rf"^\s*{re.escape(label)}\s*$"))
        )
        if not await link.count():
            raise PortalError(f"メニュー「{label}」が見つかりません。ポータルの画面構成が変わった可能性があります。")
        # サブメニューは非表示なので、クリックイベントを直接送る(onclick → フォーム送信で遷移)。
        # 画面のスクリプトが準備できる前に送ると空振りするので、遷移しなければ押し直す
        for attempt in range(3):
            await link.first.dispatch_event("click")
            try:
                await self._wait_screen(page, code, timeout_ms=12_000)
                return page
            except SessionLost:
                raise
            except PortalError:
                if attempt == 2 or not await self._is_portal(page):
                    raise
                await self._settle(page, 5_000)
        return page

    async def _open_dialog(self, page: Page, trigger, dialog_id: str) -> str:
        """trigger をクリックしてダイアログを開き、その状態の HTML を返してからダイアログを閉じる。"""
        await trigger.click()
        dialog = page.locator(f"[id='{dialog_id}'][aria-hidden='false']")
        await dialog.wait_for(state="visible")
        await self._settle(page, 5_000)
        html = await self._content(page)
        try:
            await dialog.locator(".ui-dialog-titlebar-close").first.click()
        except Exception:
            pass  # 閉じられなくても次回はトップから開き直すので問題ない
        return html

    # ------------------------------------------------------------------ 再試行の枠組み

    async def _run(self, op):
        """操作 op を排他実行する。途中でポータルからログアウトされていたら、ログインし直して1回だけやり直す。"""
        async with self._lock:
            for attempt in range(2):
                try:
                    return await op()
                except LoginRequired:
                    # 手動ログイン(capture.py)が同じプロファイルで Edge を開けるよう、こちらの Edge を閉じておく
                    await self.close()
                    raise
                except Exception as e:
                    if attempt == 0 and await self._logged_out():
                        continue  # 次の op() はトップを開き直すところから始まるので、そこで再ログインされる
                    if isinstance(e, PortalError):
                        raise
                    # Playwright のタイムアウトなど想定外の例外も、内容が Claude に伝わるよう PortalError にする
                    raise PortalError(f"ポータル操作中にエラーが発生しました: {type(e).__name__}: {str(e)[:500]}") from e

    async def _logged_out(self) -> bool:
        if not self._page or self._page.is_closed():
            return False
        try:
            return LOGGED_OUT_TEXT in await self._page.content()
        except Exception:
            return False

    # ------------------------------------------------------------------ フォーム操作の補助

    async def _select(self, page: Page, select_id: str, label: str, _retry: int = 0) -> str:
        """PrimeFaces のプルダウン(隠し select)で、表示文字が label の選択肢を選ぶ。全角/半角括弧と空白の違いは無視する。"""
        result = await page.evaluate(
            """([id, label]) => {
                const norm = t => t.replace(/（/g, '(').replace(/）/g, ')').replace(/[\\s\\u3000]/g, '');
                const s = document.getElementById(id);
                if (!s) return {status: 'no-select'};
                const opts = [...s.options];
                const o = opts.find(o => norm(o.textContent) === norm(label));
                if (!o) return {status: 'no-option', options: opts.slice(-8).map(o => o.textContent.trim())};
                s.value = o.value;
                s.dispatchEvent(new Event('change', {bubbles: true}));
                return {status: 'ok', text: o.textContent.trim()};
            }""",
            [select_id, label],
        )
        if result["status"] == "no-select":
            raise PortalError(f"選択欄 {select_id} が見つかりません。画面構成が変わった可能性があります。")
        if result["status"] != "ok":
            raise PortalError(f"「{label}」は選べません。選択肢の例: {', '.join(result['options'])}")
        await asyncio.sleep(0.5)
        await self._settle(page, 5_000)
        # 直前の Ajax でフォームが書き換わり既定値に戻ることがあるので、値が残っているか確認して必要なら選び直す
        current = await page.evaluate(
            "id => { const s = document.getElementById(id); return s ? s.options[s.selectedIndex]?.textContent.trim() : null; }",
            select_id,
        )
        if current != result["text"]:
            if _retry >= 2:
                raise PortalError(f"「{label}」を選択しても既定値に戻されてしまいます(現在: {current})。")
            return await self._select(page, select_id, label, _retry + 1)
        return result["text"]

    async def _fill(self, page: Page, input_id: str, value: str) -> None:
        box = page.locator(f"[id='{input_id}']")
        if await box.input_value() == value:
            return  # 同じ値なら触らない(change で Ajax が走りフォームが書き換わるのを避ける)
        await box.fill(value)
        await box.dispatch_event("change")
        # change で Ajax が走りフォームが書き換わることがあるので、落ち着くまで待つ
        await asyncio.sleep(0.5)
        await self._settle(page, 5_000)

    async def _submit(self, page: Page, button_id: str) -> str:
        """検索・表示ボタンを押し、Ajax の応答と画面の書き換えを待って HTML を返す。"""
        button = page.locator(f"[id='{button_id}']")
        try:
            async with page.expect_response(
                lambda r: r.request.method == "POST" and ".xhtml" in r.url, timeout=NAV_TIMEOUT_MS
            ):
                await button.click()
        except Exception:
            # 応答を取りこぼしても、書き換えが落ち着けば結果は読める
            pass
        await asyncio.sleep(0.8)
        await self._settle(page, 8_000)
        html = await self._content(page)
        if LOGGED_OUT_TEXT in html:
            raise SessionLost("ポータルから自動ログアウトされました。")
        return html

    # ------------------------------------------------------------------ 公開 API(すべて HTML を返す)

    async def top(self) -> str:
        async def op():
            return await self._content(await self._goto_top())
        return await self._run(op)

    async def timetable(self, year: int | None = None, term: str | None = None) -> str:
        """学生時間割表。year(例: 2026)・term(例: 前期(1Q)/後期(4Q)/すべて対象)を指定すると切り替えて表示する。"""
        async def op():
            page = await self._open_menu("学生時間割表", "Kmd008")
            if year is None and term is None:
                return await self._content(page)
            if year is not None:
                await self._fill(page, "funcForm:nendo_input", str(year))
            if term is not None:
                await self._select(page, "funcForm:gakki_input", term)
            return await self._submit(page, "funcForm:search")
        return await self._run(op)

    async def grades(self) -> str:
        async def op():
            page = await self._open_menu("成績メニュー", "Pkx005")
            # 成績メニューはウィザード形式。右上の「＞」を成績照会(Kmg006)が出るまで押す
            for _ in range(6):
                html = await self._content(page)
                if "[Kmg006]" in html:
                    return html
                before = SCREEN_CODE_RE.search(html)
                await page.locator(".transition-forward button").first.click()
                # 画面コードが変わるまで待つ
                loop = asyncio.get_running_loop()
                deadline = loop.time() + NAV_TIMEOUT_MS / 1000
                while loop.time() < deadline:
                    html = await self._content(page)
                    if LOGGED_OUT_TEXT in html:
                        raise SessionLost("ポータルから自動ログアウトされました。")
                    now = SCREEN_CODE_RE.search(html)
                    if now and (not before or now.group(1) != before.group(1)):
                        break
                    await asyncio.sleep(0.5)
                await self._settle(page, 5_000)
            raise PortalError("成績照会画面までたどり着けませんでした。")
        return await self._run(op)

    async def attendance(self, term: str | None = None) -> str:
        """学生出欠状況確認。term は「2026年度 前期(1Q)」の形式。省略時は画面の既定(今学期)。"""
        async def op():
            page = await self._open_menu("学生出欠状況確認", "Atb005")
            if term:
                await self._select(page, "funcForm:kaikoNendoGakki_input", term)
            return await self._submit(page, "funcForm:btnHyoji")
        return await self._run(op)

    # ---- 掲示板

    @staticmethod
    def _exact(text: str) -> re.Pattern:
        # 画面上の件名は全角空白や連続空白を含むことがあり、解析結果(空白を1つに詰めたもの)と一致しない。
        # そのため空白部分は種類・個数を問わず一致させる
        tokens = text.split()
        return re.compile(r"^\s*" + r"\s+".join(re.escape(t) for t in tokens) + r"\s*$")

    async def _bulletin_page(self, tab: str, keyword: str | None = None) -> Page:
        page = await self._open_menu("掲示板", "Bsd007")
        if keyword:
            # キーワードは件名・差出人・本文が対象。検索後もタブ構成はそのまま
            await self._fill(page, "funcForm:keyword", keyword)
            await self._submit(page, "funcForm:search")
        tab_li = page.locator("li[role='tab']").filter(has=page.locator("a", has_text=self._exact(tab)))
        if not await tab_li.count():
            raise PortalError(f"掲示板にタブ「{tab}」がありません。")
        if await tab_li.first.get_attribute("aria-selected") != "true":
            await tab_li.first.locator("a").click()
            await page.locator("li[role='tab'][aria-selected='true']").filter(
                has=page.locator("a", has_text=self._exact(tab))
            ).wait_for()
            await self._settle(page, 5_000)
            if LOGGED_OUT_TEXT in await self._content(page):
                raise SessionLost("ポータルから自動ログアウトされました。")
        return page

    async def _show_all(self, page: Page, group: str | None) -> None:
        """選択中タブで「すべて表示する」を押し、掲示が増えるまで待つ。グループタブでは group のカテゴリだけ展開する。"""
        panel = page.locator(".ui-tabs-panel[aria-hidden='false']")
        scope = panel
        if group:
            # グループタブはアコーディオン。見出し(h3)の直後の中身(div)にそのカテゴリの一覧とボタンがある
            header = panel.locator("h3.ui-accordion-header").filter(has_text=group.strip())
            if not await header.count():
                raise PortalError(f"掲示板にカテゴリ「{group}」がありません。")
            scope = header.first.locator("xpath=following-sibling::div[1]")
        button = scope.locator(f"button[title='{SHOW_ALL_TITLE}']")
        if not await button.count():
            return  # 件数が少なくボタンがない(または非表示) = すでに全件表示されている
        links = panel.locator("a.ui-commandlink")
        before = await links.count()
        # ボタンは画面外・重なりで普通のクリックが失敗することがあるので、クリックイベントを直接送る
        await button.first.dispatch_event("click")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + NAV_TIMEOUT_MS / 1000
        while loop.time() < deadline:
            await asyncio.sleep(0.7)
            if LOGGED_OUT_TEXT in await self._content(page):
                raise SessionLost("ポータルから自動ログアウトされました。")
            try:
                # フォーム全体の書き換え中はタブも一覧も一時的に消えるので、タブが戻るまでは判定しない
                if not await page.locator("li[role='tab'][aria-selected='true']").count():
                    continue
                # 全件表示になるとボタンが消える(非表示になる)。掲示が増えたことでも判定する
                if await links.count() > before:
                    break
                if not await button.count() or not await button.first.is_visible():
                    break
            except Exception:
                pass  # 一覧の書き換え中
        await self._settle(page, 5_000)

    async def bulletins(
        self, tab: str = "グループ", show_all: bool = False, group: str | None = None, keyword: str | None = None
    ) -> str:
        async def op():
            page = await self._bulletin_page(tab, keyword)
            if show_all:
                await self._show_all(page, group)
            return await self._content(page)
        return await self._run(op)

    async def _find_bulletin(self, page: Page, title: str, tab: str, nth: int, show_all: bool,
                             group: str | None, keyword: str | None):
        """件名が title の nth 番目の掲示リンクを返す。最初の表示になければ「すべて表示する」を押して探し直す。"""
        if show_all:
            await self._show_all(page, group)
        panel = page.locator(".ui-tabs-panel[aria-hidden='false']")
        links = panel.locator("a.ui-commandlink").filter(has_text=self._exact(title))
        if await links.count() <= nth and not show_all:
            # 古い掲示は最初の十数件に入っていないので、すべて表示してから探し直す
            if tab == "グループ" and not group:
                # カテゴリが分からないときは、全掲示が並ぶ「全表示」タブを全件展開して探す
                page = await self._bulletin_page("全表示", keyword)
                await self._show_all(page, None)
                panel = page.locator(".ui-tabs-panel[aria-hidden='false']")
                links = panel.locator("a.ui-commandlink").filter(has_text=self._exact(title))
            else:
                await self._show_all(page, group)
        count = await links.count()
        if count == 0:
            raise PortalError(f"タブ「{tab}」に件名「{title}」の掲示が見つかりません。")
        if nth >= count:
            raise PortalError(f"件名「{title}」は {count} 件しかありません(nth={nth})。")
        return page, links.nth(nth)

    async def bulletin_detail(
        self, title: str, tab: str = "グループ", nth: int = 0, show_all: bool = False,
        group: str | None = None, keyword: str | None = None,
        attachments: bool = True, download_dir: Path | None = None,
    ) -> dict:
        """掲示を開いて {"detail": 本文ダイアログの HTML, "files": 添付一覧の HTML or None, "saved": [保存したファイル]} を返す。

        attachments=True なら「添付資料を確認」があるときに添付一覧も開く。download_dir を渡すと添付を全部保存する。
        開いた掲示はポータル上で既読になる。
        """
        async def op():
            page = await self._bulletin_page(tab, keyword)
            page, link = await self._find_bulletin(page, title, tab, nth, show_all, group, keyword)
            await link.click()
            dialog = page.locator("[id='bsd00702:dialog'][aria-hidden='false']")
            await dialog.wait_for(state="visible")
            await self._settle(page, 5_000)
            result = {"detail": await self._content(page), "files": None, "saved": []}
            attach = dialog.locator(f"button[title='{ATTACH_TITLE}']")
            if (attachments or download_dir) and await attach.count():
                await attach.first.click()
                files = page.locator("[id='pkx02201:dialog'][aria-hidden='false']")
                await files.wait_for(state="visible")
                await self._settle(page, 5_000)
                result["files"] = await self._content(page)
                if download_dir:
                    result["saved"] = await self._download_all(page, files, download_dir)
            return result
        return await self._run(op)

    async def _download_all(self, page: Page, files, download_dir: Path) -> list[str]:
        """ファイル一覧ダイアログの各行のダウンロードボタンを押して保存し、保存先のパスを返す。"""
        download_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        rows = files.locator(".tableDownloadRow")
        for i in range(await rows.count()):
            button = rows.nth(i).locator("button:has(.fa-download)")
            if not await button.count():
                continue
            async with page.expect_download(timeout=NAV_TIMEOUT_MS) as info:
                await button.first.click()
            download = await info.value
            name = re.sub(r'[\\/:*?"<>|]', "_", download.suggested_filename) or f"attachment_{i}"
            path = download_dir / name
            stem, suffix, k = path.stem, path.suffix, 1
            while path.exists():  # 同名ファイルは上書きせず連番を付ける
                path = download_dir / f"{stem}_{k}{suffix}"
                k += 1
            await download.save_as(str(path))
            saved.append(str(path))
        return saved

    # ---- シラバス

    async def syllabus(self, course_code: str, name: str | None = None, year: int | None = None) -> str:
        """授業コード(例: A1234567)のシラバスを開いた HTML を返す。

        まず今学期の時間割から探し、なければシラバス検索(year 年度の全学期)で探す。name は授業名の一部。
        """
        code = course_code.strip()

        async def from_timetable():
            page = await self._open_menu("学生時間割表", "Kmd008")
            cell = page.locator("div.jugyo-info").filter(has_text=code)
            if not await cell.count():
                return None
            button = cell.first.locator("button[title='シラバス照会画面を表示します。']")
            return await self._open_dialog(page, button.first, "pkx02301:dialog")

        async def from_search():
            # 「授業科目」欄は授業コードでは引けないので、授業名があればそれで、なければキーワード欄で検索する
            if name:
                page = await self._syllabus_search_page(name=name, year=year, term="すべて対象")
            else:
                page = await self._syllabus_search_page(keyword=code, year=year, term="すべて対象")
            link = page.locator("a").filter(has_text=re.compile(rf"^\s*{re.escape(code)}\b"))
            if not await link.count():
                hint = "" if name else " 授業名(name)も指定すると見つかりやすくなります。"
                raise PortalError(f"授業コード {code} のシラバスが見つかりません。{hint}")
            return await self._open_dialog(page, link.first, "pkx02301:dialog")

        async def op():
            return await from_timetable() or await from_search()
        return await self._run(op)

    async def _syllabus_search_page(
        self, name: str | None = None, teacher: str | None = None, keyword: str | None = None,
        year: int | None = None, term: str | None = None,
    ) -> Page:
        page = await self._open_menu("シラバス照会", "Kmh006")
        if name:
            await self._fill(page, "funcForm:jugyoKamoku", name)
        if teacher:
            await self._fill(page, "funcForm:tantoKyoin", teacher)
        if keyword:
            await self._fill(page, "funcForm:keyword", keyword)
        if year is not None:
            await self._fill(page, "funcForm:kaikoNendo_input", str(year))
        if term:
            await self._select(page, "funcForm:kaikoGakki_input", term)
        await self._submit(page, "funcForm:search")
        return page

    async def syllabus_search(
        self, name: str | None = None, teacher: str | None = None, keyword: str | None = None,
        year: int | None = None, term: str | None = None,
    ) -> str:
        """シラバス検索の結果一覧の HTML。term は「前期(1Q)」「後期(4Q)」「すべて対象」など(省略時は画面の既定=今学期)。"""
        async def op():
            page = await self._syllabus_search_page(name, teacher, keyword, year, term)
            return await self._content(page)
        return await self._run(op)
