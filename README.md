# KUPORT-MCP

工学院大学の学生ポータル **Ku-Port** を、Claude・ChatGPT（Codex）・Antigravity などの MCP クライアントから使えるようにする非公式の MCP サーバーです。
Ku-Port には API がないため、Playwright で Microsoft Edge（または Google Chrome）を操作して画面を読み取ります。

> **非公式ツールです。** 大学・ポータル提供元とは無関係です。利用は自己責任で、大学の情報システム利用規程に従ってください。
> ログイン情報・成績などはすべて自分の PC の中だけで扱い、外部に公開しない設計にしています（後述）。

## できること（ツール）

| ツール | 内容 |
|---|---|
| `get_today` | トップ画面の今日の授業・終日のお知らせ・重要な掲示 |
| `get_timetable(day?, year?, term?)` | 時間割（曜日で絞り込み可。年度・学期を指定すれば過去の学期も） |
| `get_grades(in_progress_only?)` | 成績・GPA・単位修得状況 |
| `get_attendance(term?, course?)` | 出欠状況（授業ごとの出席率と各回の出欠） |
| `list_bulletins(tab?, show_all?, group?, keyword?, unread_only?, offset?, limit?)` | 掲示一覧（グループ/全表示/授業/時間割変更/未読 など）。`keyword` で検索、`show_all` で全件 |
| `read_bulletin(title, tab?, nth?, group?, keyword?)` | 掲示の本文と添付ファイル名（古い掲示は自動で全件表示して探す。開くと既読になる） |
| `download_bulletin_attachments(title, …)` | 掲示の添付ファイルを `downloads/` に保存 |
| `get_class_changes` | 休講・補講・教室変更の掲示 |
| `get_syllabus(course_code, name?, year?)` | シラバス（今学期の時間割になければ検索。その場合は授業名 `name` も渡す） |
| `search_syllabus(name?, teacher?, keyword?, year?, term?)` | シラバス検索（履修していない授業も） |
| `get_vacant_rooms(date?, campus?, building?, floor?, from_time?, to_time?, min_minutes?, include_bookings?)` | 空き教室（教室利用状況）。指定した時間帯に `min_minutes` 分以上連続して空いている教室と、その空き時間帯。`campus`・`building`・`floor` で絞る（絞らないと件数が多い）。休講になった授業の教室は「授業あり」のままになる |

## 必要なもの

- Windows と Microsoft Edge（既定）または Google Chrome
  - Chrome を使うときは `.env` に `KUPORT_BROWSER=chrome` と書きます。プロファイルは Edge と別の `profile-chrome/` になります（初回はログインし直しになることがあります）
- Python 3.10 以上
- Ku-Port のアカウント（統合認証のユーザー名・パスワード）

## セットアップ

```powershell
git clone https://github.com/escapefromsoftware/KU-PORT_MCP.git
cd KU-PORT_MCP
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

`.env` に次を書きます（`.env` は git 管理外です）。

```
PORTAL_URL=https://ku-port.sc.kogakuin.ac.jp/
KUPORT_ID=統合認証のユーザー名（@kogakuin.ac.jp より前）
KUPORT_PASSWORD=パスワード
```

### 初回ログイン（ワンタイムパスワード）

新しい環境では、初回に多要素認証（ワンタイムパスワード）を求められることがあります。次を実行し、開いたブラウザで手動ログインしてから、ターミナルで `q` を押して閉じてください。

```powershell
.\.venv\Scripts\python.exe capture.py
```

以降はブラウザのプロファイル（`profile/`）に認証済みの記録が残り、ID・パスワードだけで自動ログインできます。
ツールが「ワンタイムパスワードが必要です」と返したときも、同じ手順でログインし直してください。

## MCP クライアントへの登録

以下の `<PATH>` は、このリポジトリを置いたフォルダの絶対パスに置き換えてください（例: `C:\Users\you\KU-PORT_MCP`）。

> **複数のクライアントで同時に使う場合**は、クライアントごとに `KUPORT_PROFILE_DIR` で別のプロファイルを指定してください。同じプロファイルは同時に1つのブラウザしか開けません。
> 新しいプロファイルは初回に OTP が要りますが、どのブラウザも開いていないときにログイン済みの `profile/` をコピーすれば省略できます。

> ツールが最後に使われてから **60 秒**たつと、ブラウザは自動で閉じてプロファイルを手放します（`capture.py` や他のクライアントがそのまま使えます）。次の呼び出しでは自動で開き直すので、数秒余分にかかります。
> この秒数は環境変数 `KUPORT_IDLE_SECONDS` で変えられます（`0` で毎回閉じる、負の数で閉じない）。
>
> 同じプロファイルを別のクライアントが使っている最中に呼ぶと、**後から呼んだほうが優先**されます。使っている側は実行中のツールを終えてから手放し、後から呼んだ側は最大 45 秒待って起動します。
> ただし `capture.py`（手動ログイン用）は譲らないので、開いている間は閉じてから呼んでください。

### Claude Code

```powershell
claude mcp add -s user kuport -- <PATH>\.venv\Scripts\python.exe <PATH>\server.py
```

`-s user` を付けないと、コマンドを実行したフォルダ専用の登録になります。

### Claude デスクトップアプリ

設定ファイルの `mcpServers` に追加し、アプリを完全に終了（タスクトレイから「終了」）して起動し直します。
設定ファイルは「設定 → 開発者 → 構成を編集」から開けます（Microsoft Store 版では `%LOCALAPPDATA%\Claude-Data\<パッケージ名>\claude_desktop_config.json` にあることがあります）。

```json
{
  "mcpServers": {
    "kuport": {
      "command": "<PATH>\\.venv\\Scripts\\python.exe",
      "args": ["<PATH>\\server.py"],
      "env": { "KUPORT_PROFILE_DIR": "profile-desktop" }
    }
  }
}
```

### Antigravity

`%USERPROFILE%\.gemini\config\mcp_config.json` に上と同じ形式で書き（`KUPORT_PROFILE_DIR` は `profile-antigravity` など）、Antigravity を再起動します。

### ChatGPT デスクトップアプリ（Codex）/ Codex CLI

ChatGPT デスクトップアプリ（Windows では Microsoft Store の「ChatGPT」。中身は Codex と一体）と Codex CLI は、同じ設定ファイル `%USERPROFILE%\.codex\config.toml` を使います。
ChatGPT デスクトップアプリでは **Codex（ローカル実行）の会話**でのみ使えます（普通のチャットからは使えません）。

**1. Codex を入れる**（どちらか）

- ChatGPT デスクトップアプリ: Microsoft Store で「ChatGPT」を入れてサインインする
- Codex CLI: `npm install -g @openai/codex` で入れて、`codex` を実行してサインインする

**2. kuport を登録する**

Codex CLI が使えるなら、コマンド1つで登録できます。

```powershell
codex mcp add kuport --env KUPORT_PROFILE_DIR=profile-codex -- <PATH>\.venv\Scripts\python.exe <PATH>\server.py
```

`codex mcp list` に `kuport` が `enabled` で出れば登録できています。

**3. 設定を調整する（推奨）**

ブラウザ操作で1回あたり数秒〜十数秒かかるので、`config.toml` の `[mcp_servers.kuport]` にタイムアウトを足しておくと安心です。
CLI を使わない場合は、次の内容を `config.toml` に直接書いても登録できます。

```toml
[mcp_servers.kuport]
command = '<PATH>\.venv\Scripts\python.exe'
args = ['<PATH>\server.py']
startup_timeout_sec = 30
tool_timeout_sec = 120
# 毎回の承認を省略したい場合のみ(副作用は掲示の既読化と downloads/ への保存だけ)
# default_tools_approval_mode = "approve"

[mcp_servers.kuport.env]
KUPORT_PROFILE_DIR = "profile-codex"
```

`default_tools_approval_mode` は `[mcp_servers.kuport.env]` より上に書いてください（下に書くと環境変数として扱われ、効きません）。

**4. 使う**

ChatGPT デスクトップアプリ（または Codex CLI）を再起動し、Codex の**新しいスレッド**で「今日の授業教えて」などと聞きます。
新しいプロファイル（`profile-codex`）は初回にワンタイムパスワードが要るので、「ワンタイムパスワードが必要です」と出たら次でログインしてください。

```powershell
$env:KUPORT_PROFILE_DIR = "profile-codex"; .\.venv\Scripts\python.exe capture.py
```

## スマホから使う

スマホのアプリから PC 上のエージェントを遠隔操作する方法なら、サーバーをインターネットに公開せずに使えます。
処理はすべて PC で動くので、使う間は **PC を起動したまま（スリープしない設定）** にしてください。

### Claude（Claude Code の Remote Control）

1. PC で、`.mcp.json` などで kuport を登録したフォルダに移動して Remote Control を起動します（起動中のセッションで `/remote-control` と打っても可）。
   ```powershell
   cd <kuport を登録したフォルダ>
   claude remote-control
   ```
   `Connected` と表示されれば待ち受け状態です。Spawn mode は `same-dir` にしてください（`w` で切り替え）。プロジェクト単位で登録した kuport を確実に読み込むためです。
2. スマホの Claude アプリで「Code」タブを開き、一覧からそのセッションを選びます（ターミナルでスペースキーを押すと出る QR コードを読み取っても可）。
3. 新しいセッションで「今日の授業教えて」などと聞きます。

ターミナルを閉じるとセッションも終わります。詳しくは [Claude Code Docs: Remote Control](https://code.claude.com/docs/en/remote-control) を参照してください。

### ChatGPT（Codex のリモート）

1. PC で、`config.toml` に kuport を登録した ChatGPT デスクトップアプリ（Codex）を起動しておきます。
2. スマホの Codex のリモート機能で、接続先としてその PC を選びます。
3. 「今日の授業教えて」などと聞きます。処理は PC 上の Codex で動き、kuport もそのまま使えます。

### Gemini（Antigravity の Remote Control）

Antigravity の Remote Control を使うと、スマホのブラウザから PC 上の Antigravity のセッションを操作できます（同じ Google アカウントでログインが必要）。
詳しくは [Antigravity Docs: Remote Control](https://antigravity.google/docs/remote-control) を参照してください。Gemini アプリ単体からは使えません。

### Web 版（ChatGPT / Claude / Gemini）について

Web 版はクラウド上で動くため、PC の中のこのサーバーには接続できません。
使うにはサーバーをインターネットに公開する必要がありますが、成績やログイン情報を扱うため**推奨しません**。

## 注意

- `.env`・`profile*/`（Cookie）・`dumps/`・`fixtures/`（画面の HTML）・`downloads/`（添付ファイル）は個人情報を含むため git 管理外です。共有しないでください。
- 自分のブラウザで同時に Ku-Port を使うと、どちらかのセッションが切られることがあります（サーバー側は再ログインして1回やり直します）。
- `read_bulletin` で開いた掲示はポータル上で既読になります。
- ポータルの画面が変わると解析が壊れることがあります。`capture.py` で画面を保存して原因を調べられます。

## 開発

- `server.py` … MCP サーバー（ツール定義）
- `kuport/portal.py` … ブラウザ操作（ログイン・画面遷移・HTML 取得）
- `kuport/parsers.py` … HTML → 構造化データ
- `capture.py` … 画面 HTML の保存・手動ログイン用ツール

画面の判別は URL ではなく本文中の画面コード（`[Bsd007]` など）で行い、自動採番の要素 ID（`j_idt…`）には依存しません。

解析のテストは実際の画面 HTML（個人の成績・出欠などを含む）に依存するため、リポジトリには含めていません。
解析を直すときは、`capture.py` で自分の画面を保存し、`kuport/parsers.py` の関数に通して確認してください。

## ライセンス

[MIT License](LICENSE)
