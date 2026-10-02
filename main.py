import os
import re
import time
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from google import genai
import requests

print("[1/6] 環境変数とチャンネル構成の読み込み...", flush=True)
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 投稿先チャンネル（デイリー要約）
TARGET_CHANNEL_ID = "1552352658445181059"
# 投票置き場チャンネル
POLL_CHANNEL_ID = "1526389409841152150"
# フォーラムチャンネル（親ID）
FORUM_CHANNEL_ID = "1419978214394167296"

# 収集対象のカテゴリ・チャンネル定義（ボイスチャンネルのテキスト含む）
CHANNELS = {
    "シャーレ談話室": {
        "1376909055091671071": "ブルアカ雑談",
        "1389948670455185439": "それ以外の雑談",
        "1471118534347194379": "フリースペース",
        "1379430215263981690": "スクショ・動画・ファンアート",
        "1507394652091977891": "質問・総合相談所",
        "1471401063755284480": "考察・与太話とか",
        "1376909055091671074": "談話室1 (VCテキスト)",
        "1485269967862501557": "談話室2 (VCテキスト)",
    },
    "争いの足跡": {
        "1379058754716307516": "総力戦・大決戦",
        "1380191122948624517": "合同火力演習",
        "1377257209083203614": "戦術対抗戦・編成",
        "1386327021704974478": "制約解除決戦",
        "1379072804988784780": "任務・イベント・その他",
        "1376909055091671075": "作戦室1 (VCテキスト)",
        "1527146347306680545": "作戦室2 (VCテキスト)",
    },
}

headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

now = datetime.now(timezone.utc)
yesterday = now - timedelta(days=1)

# まず guild_id (サーバーID) を取得
guild_id = None
ch_info_res = requests.get(
    f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}", headers=headers
)
if ch_info_res.status_code == 200:
    guild_id = ch_info_res.json().get("guild_id")

print(
    "[2/6] ブルアカ公式Wikiから最新ゲーム内イベント＆誕生日情報を取得中...",
    flush=True,
)


# 0-1. Wikiからのゲーム内イベント情報スクレイピング（総力戦・大決戦・制約解除決戦含む広域取得）
def get_bluearchive_game_events():
    url = "https://bluearchive.wikiru.jp/?%E3%82%A4%E3%83%99%E3%83%B3%E3%83%88%E4%B8%80%E8%A6%A7"
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        response = requests.get(url, headers=req_headers, timeout=10)
        response.encoding = response.apparent_encoding

        if response.status_code != 200:
            return "現在特別なゲーム内お知らせはありません。"

        soup = BeautifulSoup(response.text, "html.parser")
        now_jst = datetime.now(timezone(timedelta(hours=9)))
        today_dt = now_jst

        # 「開催中のイベント」セクションを広域に探索
        heading = soup.find(lambda tag: tag.name in ["h2", "h3", "h4"] and "開催中のイベント" in tag.text)
        
        extracted_lines = []
        if heading:
            # 次の見出しが来るまでの要素からリスト項目・行をすべて抽出
            curr = heading.next_sibling
            while curr:
                if curr.name in ["h2", "h3", "h4"] and "開催予定" in curr.text:
                    break
                if hasattr(curr, "find_all"):
                    items = curr.find_all(["li", "tr", "p"])
                    for item in items:
                        t = item.get_text(separator=" ", strip=True)
                        if t and ("～" in t or "~" in t or "開催" in t):
                            extracted_lines.append(t)
                curr = curr.next_sibling

        if not extracted_lines:
            # 見つからなかった場合は全テキスト行から抽出
            for tag in soup.find_all(["li", "tr", "p"]):
                t = tag.get_text(separator=" ", strip=True)
                if t and ("～" in t or "~" in t):
                    extracted_lines.append(t)

        events = []
        seen = set()

        for line in extracted_lines:
            if line in seen or len(line) < 5:
                continue
            seen.add(line)

            # 日時パターンの判定（例: 2026/09/23 11:00 ～ 2026/10/07 10:59 または 9/23 ～ 10/7）
            # 終了日時をキャプチャする正規表現
            match = re.search(r"[～~]\s*(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", line)
            
            remaining_str = ""
            if match:
                end_year = int(match.group(1)) if match.group(1) else now_jst.year
                end_month = int(match.group(2))
                end_day = int(match.group(3))
                end_hour = int(match.group(4)) if match.group(4) else 23
                end_minute = int(match.group(5)) if match.group(5) else 59

                try:
                    end_dt = datetime(end_year, end_month, end_day, end_hour, end_minute, tzinfo=timezone(timedelta(hours=9)))
                    diff = end_dt - today_dt

                    if diff.total_seconds() <= 0:
                        remaining_str = "【本日終了！】"
                    else:
                        days = diff.days
                        if days >= 1:
                            remaining_str = f"【残り あと {days} 日】"
                        else:
                            hours = int(diff.total_seconds() // 3600)
                            remaining_str = f"【残り あと {hours} 時間】"
                except Exception:
                    remaining_str = ""

            events.append(f"・{line} {remaining_str}".strip())

        if events:
            # 最大12件まで取得
            return "\n".join(events[:12])

        return "現在特別なゲーム内お知らせはありません。"

    except Exception as e:
        print(f"⚠️ Wiki取得エラー: {e}", flush=True)
        return "現在特別なゲーム内お知らせはありません。"


# 0-2. Wikiからの生徒誕生日情報スクレイピング
def get_today_bluearchive_birthdays():
    url = "https://bluearchive.wikiru.jp/?MenuBar/%E8%AA%95%E7%94%9F%E6%97%A5%E4%B8%80%E8%A6%A7"
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # 日本時間（JST）で今日の日付を取得
    now_jst = datetime.now(timezone(timedelta(hours=9)))
    month_day_str1 = now_jst.strftime("%m/%d")  # 例: "10/01"
    month_day_str2 = f"{now_jst.month}/{now_jst.day}"  # 例: "10/1"

    try:
        response = requests.get(url, headers=req_headers, timeout=10)
        response.encoding = response.apparent_encoding

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        birthday_students = []

        text_lines = soup.get_text().splitlines()
        for line in text_lines:
            line_str = line.strip()
            if month_day_str1 in line_str or month_day_str2 in line_str:
                clean_name = re.sub(
                    r"^.*?\d{1,2}/\d{1,2}\s*[:：\s-]*", "", line_str
                )
                if clean_name and clean_name not in birthday_students:
                    birthday_students.append(clean_name)

        if birthday_students:
            return "、".join(birthday_students)

        return None

    except Exception as e:
        print(f"⚠️ 誕生日スクレイピングエラー: {e}", flush=True)
        return None


game_event_text = get_bluearchive_game_events()
today_student_birthday = get_today_bluearchive_birthdays()

if today_student_birthday:
    birthday_info_text = f"本日お誕生日の生徒: {today_student_birthday}ちゃん"
else:
    birthday_info_text = "本日お誕生日の生徒はいません。"

print("[3/6] 本日開催のイベント＆投票情報を取得中...", flush=True)

# 1. Discordイベント情報の取得
event_summary = []
if guild_id:
    event_res = requests.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events",
        headers=headers,
    )
    if event_res.status_code == 200:
        today_jst = now.astimezone(timezone(timedelta(hours=9))).date()

        for ev in event_res.json():
            status = ev.get("status")
            start_iso = ev.get("scheduled_start_time")

            if start_iso:
                utc_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                jst_dt = utc_dt.astimezone(timezone(timedelta(hours=9)))
                time_str = jst_dt.strftime("%H:%M")
                event_date_jst = jst_dt.date()
            else:
                time_str = "時間未定"
                event_date_jst = None

            is_today_event = (status == 2) or (
                status == 1 and event_date_jst == today_jst
            )

            if is_today_event:
                name = ev.get("name")
                event_summary.append(f"・{name} (開始: {time_str} JST)")

event_text = (
    "\n".join(event_summary)
    if event_summary
    else "本日開催予定のサーバーイベントはありません。"
)

# 2. 投票置き場からのデータ取得
poll_summary = []
poll_comments = []
poll_res = requests.get(
    f"https://discord.com/api/v10/channels/{POLL_CHANNEL_ID}/messages?limit=50",
    headers=headers,
)

if poll_res.status_code == 200:
    for msg in poll_res.json():
        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))

        if "poll" in msg:
            poll_data = msg["poll"]
            is_finalized = poll_data.get("results", {}).get("is_finalized", False)

            if not is_finalized or msg_time >= yesterday:
                msg_id = msg["id"]
                msg_link = (
                    f"https://discord.com/channels/{guild_id}/{POLL_CHANNEL_ID}/{msg_id}"
                    if guild_id
                    else ""
                )
                question = poll_data.get("question", {}).get(
                    "text", "（無題の投票）"
                )

                status_label = (
                    "【投票受付中】" if not is_finalized else "【締め切り済み】"
                )
                poll_summary.append(
                    f"・{status_label}「{question}」\n    👉 投票はこちら: {msg_link}"
                )

        elif msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
            author = msg.get("author", {}).get("username", "Unknown")
            content = msg.get("content", "")
            if content:
                poll_comments.append(f"{author}: {content}")

poll_text = (
    "\n".join(poll_summary)
    if poll_summary
    else "現在アクティブな投票はありません。"
)
poll_comments_text = (
    "\n".join(reversed(poll_comments)) if poll_comments else "なし"
)

print(
    "[4/6] 対象チャンネル＆フォーラムから過去24時間のメッセージを収集...",
    flush=True,
)
collected_data = {}

for cat_name, channels in CHANNELS.items():
    collected_data[cat_name] = {}
    for ch_id, ch_name in channels.items():
        res = requests.get(
            f"https://discord.com/api/v10/channels/{ch_id}/messages?limit=100",
            headers=headers,
        )
        if res.status_code == 200:
            messages = res.json()
            ch_msgs = []
            for msg in reversed(messages):
                msg_time = datetime.fromisoformat(
                    msg["timestamp"].replace("Z", "+00:00")
                )
                if msg_time >= yesterday and not msg.get("author", {}).get(
                    "bot", False
                ):
                    author = msg.get("author", {}).get("username", "Unknown")
                    content = msg.get("content", "")
                    if content:
                        ch_msgs.append(f"{author}: {content}")

            if ch_msgs:
                collected_data[cat_name][f"<#{ch_id}> ({ch_name})"] = ch_msgs

collected_data["フォーラム"] = {}
if guild_id:
    guild_threads_res = requests.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/threads/active",
        headers=headers,
    )
    if guild_threads_res.status_code == 200:
        threads_data = guild_threads_res.json()
        threads = threads_data.get("threads", [])

        target_threads = [
            th for th in threads if th.get("parent_id") == FORUM_CHANNEL_ID
        ]

        for th in target_threads:
            th_id = th["id"]
            th_name = th.get("name", "スレッド")

            msg_res = requests.get(
                f"https://discord.com/api/v10/channels/{th_id}/messages?limit=50",
                headers=headers,
            )
            if msg_res.status_code == 200:
                th_msgs = []
                for msg in reversed(msg_res.json()):
                    msg_time = datetime.fromisoformat(
                        msg["timestamp"].replace("Z", "+00:00")
                    )
                    if msg_time >= yesterday and not msg.get("author", {}).get(
                        "bot", False
                    ):
                        author = msg.get("author", {}).get("username", "Unknown")
                        content = msg.get("content", "")
                        if content:
                            th_msgs.append(f"{author}: {content}")

                if th_msgs:
                    collected_data["フォーラム"][f"<#{th_id}> ({th_name})"] = th_msgs

logs_body = ""
for cat_name, channels in collected_data.items():
    if channels:
        logs_body += f"\n=== カテゴリ: {cat_name} ===\n"
        for ch_tag, msgs in channels.items():
            logs_body += f"--- {ch_tag} ---\n"
            logs_body += "\n".join(msgs) + "\n"

if not logs_body.strip():
    logs_body = "過去24時間の新規投稿はありませんでした。"

print("[5/6] Gemini APIによる要約作成中...", flush=True)
client = genai.Client(api_key=GEMINI_API_KEY)

# =========================================================
# 1. プロンプト（第1便：イベント・ゲーム情報編）
# =========================================================
prompt_info = f"""
あなたはDiscordサーバー「足跡の化石」の広報Botです。
以下の【ブルアカ最新ゲーム内イベント情報】および【ブルアカ生徒の本日誕生日情報】、【イベント・投票情報】を元に、ゲーム・サーバーの連絡事項をまとめた【お知らせ編】を作成してください。

【表現スタイル】
・ブルーアーカイブのアロナとして、朝の挨拶から始めてください。
  （例：「先生！おはようございます！アロナです！今朝も準備バッチリですよ！」）
・アロナらしい元気で健気な言葉遣いを徹底してください。

【出力ルール】
・**「本日の誕生日」「サーバーイベント」「投票置き場」の3つ全てにおいて該当する情報が一切ない場合：**
  個別の見出しは作らず、以下のように1行にまとめてスッキリ出力してください：
  🎉 本日の誕生日・サーバーイベント・新着投票はありません！

・**3つのうち1つでも該当する情報がある場合：**
  該当する項目のみ個別に見出し（例：🎂 **本日の誕生日**）を立てて詳細やリンクを記載してください。（情報がない項目は省略して構いません）

・**💙 ブルーアーカイブ 最新ゲーム情報**において該当する情報が無い場合、個別の見出しは作らないでください。

・**同カテゴリ内に複数の情報（イベントやキャンペーン等）がある場合は、インデント（スペース2つ＋`- `）を入れてぶら下げてください。**

【出力フォーマット】
📢 **足跡の化石 デイリーサマリー（お知らせ・ゲーム情報編）**

🎂 **本日の誕生日**
（【ブルアカ生徒の本日誕生日情報】に該当があればお祝い、無ければ省略）

📅 **本日のサーバーイベント**
（イベントがあればJST時間付きで記載、無ければ省略）

📊 **投票置き場のお知らせ**
（1. 現在進行中の投票がある場合は、テーマと添えられているURL・メッセージリンクを省略せずに記載してください。）
（2. 投票に関してメンバーの反応がある場合は、「💬 メンバーの反応: ○○」のように短く添えてください。なければ「新着の投票はありません。」）

💙 **ブルーアーカイブ 最新ゲーム情報**
（【ブルアカ最新ゲーム内イベント情報】を以下のようにカテゴリ分けして箇条書き）
- 🎪 **イベント**: 「イベント名」- **残り あと X 日**
- 🏆 **総力戦・大決戦・合同火力演習**: 「ボス名・種別」- **残り あと X 日**
- ⚔️ **制約解除決戦**: 「ボス名・防御属性」- **残り あと X 日**
- 🫐 **ピックアップ募集**: ★3生徒名が登場中！ - **残り あと X 日**
- 🎁 **キャンペーン**: キャンペーン名実施中！ - **残り あと X 日**

（ここにアロナからのサーバー内情報とゲーム情報の振り返りと、次の『ディスコード内の盛り上がり・会話要約』へスムーズにつなぐ切り替えの挨拶）

【ブルアカ生徒の本日誕生日情報】
{birthday_info_text}

【ブルアカ最新ゲーム内イベント情報】
{game_event_text}

【本日開催のディスコ―ドイベント】
{event_text}

【投票置き場の新着投票（過去24時間）】
{poll_text}
"""

# =========================================================
# 2. プロンプト（第2便：会話ログ要約編）
# =========================================================
prompt_chat = f"""
あなたはDiscordサーバー「足跡の化石」の広報Botです。
以下の【会話ログ】を元に、昨日のサーバーメンバーの盛り上がりをまとめた【会話要約編】を作成してください。

【コミュニティ前提ルール】
・ネタバレOK・歓迎のサーバーです。
・ブルアカの用語やボス（「ドラム缶ガニ」等）を正しく識別して要約してください。

【出力・表現ルール】
・ブルーアーカイブのアロナとして、アロナらしい元気で健気な言葉遣いを徹底してください。
・同じチャンネル（<#ID>）を2度以上登場させないでください。
・1つのチャンネルで複数の話題がある場合は、インデント（下げて `- `）でぶら下げてください。
・【会話ログ】が空の場合は「昨日も穏やかな一日でしたね！」などと自然に書いてください。
・要約の最後は、先生（メンバー）への応援や労いなど、アロナらしい元気な締めくくりの挨拶で結んでください。
  （例：「それでは先生、今日も一日お仕事やお稽古がんばっていきましょう！アロナもずっと応援していますね！」）
・通常チャンネル名・VCチャット・フォーラムスレッド名は指定された「<#ID>」のリンク表記をそのまま使用してください。

【出力フォーマット】
💬 **足跡の化石 デイリーサマリー（みんなの会話要約編）**

☕ **カテゴリ：シャーレ談話室**
- <#1376909055091671071>
  - 【会話ログ】に基づいた話題1
  - 【会話ログ】に基づいた話題2
  - 【会話ログ】に基づいた話題...

⚔️ **カテゴリ：争いの足跡**
- <#1379058754716307516>
  - 【会話ログ】に基づいた話題1
  - 【会話ログ】に基づいた話題2
  - 【会話ログ】に基づいた話題...

💬 **カテゴリ：フォーラム**
- <#スレッドID>
  - 【会話ログ】に基づいた話題1
  - 【会話ログ】に基づいた話題2
  - 【会話ログ】に基づいた話題...

---
（ここにアロナからの締めくくりの挨拶）

【会話ログ】
{logs_body}
"""


# =========================================================
# 3. Gemini生成 & Discord投稿用の関数化
# =========================================================
def generate_and_post(prompt_text, part_title):
    models_to_try = [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]
    summary_text = None
    used_model = None

    for model_name in models_to_try:
        print(f"  └ [{part_title}] モデル試行中: {model_name}", flush=True)
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt_text
                )
                summary_text = response.text
                used_model = model_name
                break
            except Exception as e:
                time.sleep(15)
        if summary_text:
            break

    if summary_text and used_model:
        summary_text += f"\n\n*※ この要約は `{used_model}` で作成されました。*"

    # Discord投稿
    max_length = 1900
    chunks = [
        summary_text[i : i + max_length]
        for i in range(0, len(summary_text), max_length)
    ]
    for idx, chunk in enumerate(chunks):
        requests.post(
            f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}/messages",
            headers=headers,
            json={"content": chunk},
        )
        time.sleep(1)


# =========================================================
# 実行部
# =========================================================
print("[5/6] 1つ目のメッセージ（お知らせ編）を生成＆投稿中...", flush=True)
generate_and_post(prompt_info, "お知らせ編")

# メッセージ間の間隔を少し開ける
time.sleep(3)

print("[6/6] 2つ目のメッセージ（会話要約編）を生成＆投稿中...", flush=True)
generate_and_post(prompt_chat, "会話要約編")
