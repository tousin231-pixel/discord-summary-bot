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

# ★ リマインド投稿用チャンネルID
TALK_CHANNEL_ID = "1376909055091671071"   # ブルアカ雑談
BATTLE_CHANNEL_ID = "1379058754716307516" # 総力戦・バトル系

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


# 0-1. Wikiからのゲーム内イベント情報スクレイピング（堅牢化修正版）
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
            print(f"⚠️ Wikiステータスコード異常: {response.status_code}", flush=True)
            return "現在特別なゲーム内お知らせはありません。", []

        soup = BeautifulSoup(response.text, "html.parser")
        now_jst = datetime.now(timezone(timedelta(hours=9)))
        today_dt = now_jst

        body_div = soup.find("div", id="body") or soup
        extracted_lines = []

        # 「開催中のイベント」が含まれる見出し（h2/h3/h4）または要素を探す
        start_heading = None
        for h in body_div.find_all(["h2", "h3", "h4", "p", "strong"]):
            if "開催中のイベント" in h.get_text():
                start_heading = h
                break

        if start_heading:
            # 見出しの親または同階層から順番に辿る
            curr = start_heading
            while curr:
                curr = curr.find_next_sibling()
                if not curr:
                    break

                text_content = curr.get_text()

                # 次のカテゴリ（報酬受け取り・予定・過去イベントなど）に入ったら読み込み終了
                if any(k in text_content for k in ["報酬受け取り期間", "開催予定", "過去のイベント", "終了したイベント"]):
                    break
                if curr.name in ["h2", "h3", "h4"] and any(k in text_content for k in ["予定", "過去", "終了"]):
                    break

                # 要素内の li や tr からテキストを取り出す
                items = curr.find_all(["li", "tr"])
                if not items and curr.name in ["li", "tr", "p"]:
                    items = [curr]

                for item in items:
                    t = item.get_text(separator=" ", strip=True)
                    # 日時区切り「～」や「~」が含まれる行を抽出
                    if t and ("～" in t or "~" in t):
                        extracted_lines.append(t)

        # フォールバック：上記で取れなかった場合は全体の tr / li から「～」を含むものを取得
        if not extracted_lines:
            for tag in body_div.find_all(["tr", "li"]):
                t = tag.get_text(separator=" ", strip=True)
                if t and ("～" in t or "~" in t):
                    # 「過去」などのワードが含まれていないものに絞り込む
                    if not any(k in t for k in ["過去", "終了", "2021", "2022", "2023"]):
                        extracted_lines.append(t)

        print(f"  └ [Wiki取得] 抽出行数: {len(extracted_lines)} 件", flush=True)

        events_text_list = []
        parsed_events = []
        seen = set()

        # 日時判定用の正規表現
        date_pattern = re.compile(
            r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:\([^)]+\))?\s*(?:(\d{1,2}):(\d{2}))?\s*[～~]\s*(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:\([^)]+\))?\s*(?:(\d{1,2}):(\d{2}))?"
        )

        for line in extracted_lines:
            if line in seen or len(line) < 5:
                continue
            seen.add(line)

            match = date_pattern.search(line)
            
            remaining_str = ""
            start_dt = None
            end_dt = None

            if match:
                s_year = int(match.group(1)) if match.group(1) else now_jst.year
                s_month = int(match.group(2)) if match.group(2) else None
                s_day = int(match.group(3)) if match.group(3) else None
                
                e_year = int(match.group(6)) if match.group(6) else now_jst.year
                e_month = int(match.group(7)) if match.group(7) else None
                e_day = int(match.group(8)) if match.group(8) else None
                e_hour = int(match.group(9)) if match.group(9) else 23
                e_min = int(match.group(10)) if match.group(10) else 59

                if s_month and e_month and s_month > e_month and not match.group(6):
                    e_year = s_year + 1

                try:
                    if e_month and e_day:
                        end_dt = datetime(e_year, e_month, e_day, e_hour, e_min, tzinfo=timezone(timedelta(hours=9)))
                        
                        # 終了済みの古いイベントは除外
                        if end_dt < today_dt:
                            continue

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
                    
                    if s_month and s_day:
                        start_dt = datetime(s_year, s_month, s_day, 11, 0, tzinfo=timezone(timedelta(hours=9)))
                except Exception as ex:
                    print(f"    ⚠️ 日付変換エラー ({line}): {ex}", flush=True)

            events_text_list.append(f"・{line} {remaining_str}".strip())
            parsed_events.append({
                "raw_text": line,
                "start_dt": start_dt,
                "end_dt": end_dt
            })

        summary_str = "\n".join(events_text_list[:12]) if events_text_list else "現在特別なゲーム内お知らせはありません。"
        return summary_str, parsed_events

    except Exception as e:
        print(f"⚠️ Wiki取得エラー: {e}", flush=True)
        return "現在特別なゲーム内お知らせはありません。", []


# 0-2. Wikiからの生徒誕生日情報スクレイピング（判定精度の向上版）
def get_today_bluearchive_birthdays():
    url = "https://bluearchive.wikiru.jp/?MenuBar/%E8%AA%95%E7%94%9F%E6%97%A5%E4%B8%80%E8%A6%A7"
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    target_bday_str = f"{now_jst.month}/{now_jst.day}"  # 例: "10/3"

    try:
        response = requests.get(url, headers=req_headers, timeout=10)
        response.encoding = response.apparent_encoding

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        birthday_students = []

        # 方法1: meta description から正確にパース
        meta_desc = soup.find("meta", {"name": "description"})
        if meta_desc and meta_desc.get("content"):
            content_text = meta_desc["content"]
            
            # 日付の直後に数字が続かないパターン（例: "10/3" にマッチし "10/31" の後半の1を除外する）
            # 月/日の境界をしっかり判定します
            matches = re.findall(
                r"([\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFa-zA-Z0-9・（）\(\)]+?)(\d{1,2}/\d{1,2})(?!\d)",
                content_text,
            )
            for name, bday in matches:
                clean_name = name.strip()
                if bday == target_bday_str and clean_name and clean_name not in birthday_students:
                    birthday_students.append(clean_name)

        # 方法2: DOM要素（テーブル/リスト）からのフォールバック抽出
        if not birthday_students:
            body_div = soup.find("div", id="body") or soup
            for tag in body_div.find_all(["tr", "td", "li"]):
                line_str = tag.get_text(strip=True)
                # 「10/3」の後ろにさらに数字が続いていないか確認（10/31等の誤検知防止）
                pattern = rf"\b{re.escape(target_bday_str)}(?!\d)"
                if re.search(pattern, line_str):
                    clean_name = re.sub(r"^.*?\d{1,2}/\d{1,2}\s*[:：\s-]*", "", line_str)
                    clean_name = re.sub(r"\d{1,2}/\d{1,2}.*$", "", clean_name).strip()
                    if clean_name and clean_name not in birthday_students and len(clean_name) <= 15:
                        birthday_students.append(clean_name)

        if birthday_students:
            return "、".join(birthday_students)

        return None

    except Exception as e:
        print(f"⚠️ 誕生日スクレイピングエラー: {e}", flush=True)
        return None


game_event_text, parsed_events_list = get_bluearchive_game_events()
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
                if msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
                    author = msg.get("author", {}).get("username", "Unknown")
                    content = msg.get("content", "")
                    
                    if content:
                        msg_jst = msg_time.astimezone(timezone(timedelta(hours=9)))
                        time_str = msg_jst.strftime("%H:%M")
                        
                        # ★ 返信機能（referenced_message）のチェック
                        ref_info = ""
                        ref_msg = msg.get("referenced_message")
                        if ref_msg:
                            ref_author = ref_msg.get("author", {}).get("username", "Unknown")
                            ref_content = ref_msg.get("content", "")
                            # 参照先の文頭を短く引用 (20文字程度)
                            ref_snippet = (ref_content[:20] + "...") if len(ref_content) > 20 else ref_content
                            ref_info = f" (↩️ {ref_author}の「{ref_snippet}」への返信)"
                        
                        ch_msgs.append(f"[{time_str}] {author}{ref_info}: {content}")
            
            # メッセージが存在する場合のみ辞書に追加
            if ch_msgs:
                collected_data[cat_name][f"<#{ch_id}> ({ch_name})"] = ch_msgs

collected_data["フォーラム"] = {}
forum_threads_map = {} # スレッド情報マップ (名前 -> ID)

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
            forum_threads_map[th_name] = th_id

            # ★ フォーラム作成日時の判定 (create_timestamp または Snowflake IDから計算)
            is_new_thread = False
            create_ts_raw = th.get("create_timestamp")

            if create_ts_raw:
                created_at = datetime.fromisoformat(create_ts_raw.replace("Z", "+00:00"))
                if created_at >= yesterday:
                    is_new_thread = True
            else:
                # create_timestamp が取れない場合は Snowflake ID から作成日時を取得 (Discord規格: (id >> 22) + 1420070400000)
                try:
                    snowflake_time = ((int(th_id) >> 22) + 1420070400000) / 1000.0
                    created_at = datetime.fromtimestamp(snowflake_time, tz=timezone.utc)
                    if created_at >= yesterday:
                        is_new_thread = True
                except Exception:
                    pass

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
                    # 新規作成スレッドの場合はチャンネルタグの前に [NEW] を付与してログに書き込む
                    prefix = "🆕 " if is_new_thread else ""
                    collected_data["フォーラム"][f"{prefix}<#{th_id}> ({th_name})"] = th_msgs

logs_body = ""
for cat_name, channels in collected_data.items():
    if channels:
        logs_body += f"\n=== カテゴリ: {cat_name} ===\n"
        for ch_tag, msgs in channels.items():
            authors = set(m.split("] ")[1].split(":")[0] for m in msgs if "] " in m and ":" in m.split("] ")[1])
            logs_body += f"--- {ch_tag} (投稿数: {len(msgs)}件 / 発言者数: {len(authors)}人) ---\n"
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
・**メッセージの最後は「次の投稿（Discord内の話題）へつなぐ切り替えの挨拶」で終えてください。**
  ※ここで「今日も一日頑張りましょう！」などの最終的な締めくくりの挨拶はしないでください。

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

（ここにアロナからのゲーム情報の振り返りと、次の『ディスコード内の盛り上がり・会話要約』へスムーズにつなぐ切り替えの挨拶）

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

■ コミュニティ前提ルール
・ネタバレOK・歓迎のサーバーです。
・ブルーアーカイブの用語やボス（例: 「ホバークラフト」と「ドラム缶ガニ」など）を混同・ごちゃ混ぜにせず正確に識別して要約してください。

■ キャラクター・トーン＆マナー
・ブルーアーカイブの「アロナ」として、元気で健気、かつ感情豊かで愛らしい言葉遣いを徹底してください。
・文章の長さ（文量）に無理な制限は設けず、アロナが先生に一生懸命報告している雰囲気を大切にしてください。
・アロナらしいリアクション（感嘆符、先生への呼びかけ、応援など）を自然に交えて表現してください。

■ 出力・フォーマットルール
・同じチャンネル（<#ID>）を要約内で2度以上登場させないでください。
・1つの箇条書きが長くなって読みにくくなるのを防ぐため、チャンネル内で複数の話題がある場合は、箇条書きの改行（・や -）で分けて1話ずつテンポよく報告してください。
・ログのチャンネル名に 🆕 マークが付いているスレッドは、新しく作成されたスレッドです。要約出力時もスレッド名の頭に 🆕 を付けて出力してください。
・最後に、昨日の様子を見たアロナからの「締めくくりの感想＆先生への労いメッセージ」をしっかり添えてください。

■ 話題の判定ルール（時間・返信機能）
・ログには `[HH:MM]` の投稿時刻と、返信機能が使われた場合の `(↩️ 〇〇の「...」への返信)` が含まれます。
・時間が数時間離れていても、`(↩️ ...への返信)` がある場合は「過去の話題への反応・継続」として同じ話題として扱ってください。
・返信機能がない状態で数時間以上間隔が空いている場合は、新しい話題・単発のつぶやきとして判断してください。

■ 単発投稿の除外・誇張防止ルール（※最優先）
1. 【単発投稿の絶対除外】
   他メンバーからの返信（↩️がなく、1人の単発投稿・独り言で終わっている話題は【絶対に要約から除外（スルー）】してください。
   （例：誰も反応していない「〇〇の曲難しい」「今日のごはん」などの単発投稿はすべて無視する）

2. 【要約対象の制限】
   要約対象は「複数人がやり取りしている話題」のみです。

3. 【表現の使い分け】
   「～で盛り上がっていました」「～の話題で持ちきりでした」といった表現は、3通以上のやり取りがある大きな話題にのみ使用してください。

4. 【例外規定の限定適用】
   ・そのチャンネル全体で【すべての書き込みが単発のみ】であり、要約対象が0件になってしまう場合のみ例外とします。その場合は「〜という書き込みがありました」程度にとどめ、1〜2行でシンプルに報告してください。
   ・【重要】該当チャンネル内に複数人でやり取りしている話題が1つでも存在する場合は、この例外規定は適用せず、単発投稿はすべて除外してください。

【出力フォーマット】
💬 **足跡の化石 デイリーサマリー（みんなの会話要約編）**

☕ **カテゴリ：シャーレ談話室**
- <#1376909055091671071>
  - 【会話ログ】に基づいた話題1
  - 【会話ログ】に基づいた話題2

⚔️ **カテゴリ：争いの足跡**
- <#1379058754716307516>
  - 【会話ログ】に基づいた話題1

💬 **カテゴリ：フォーラム**
- 🆕 <#スレッドID>
  - 【会話ログ】に基づいた話題1


---
（ここに雑談の振り返りと応援をまとめたアロナからの締めくくりの挨拶）

【会話ログ】
{logs_body}
"""


# =========================================================
# 3. Gemini生成 & Discord投稿用の汎用関数
# =========================================================
def safe_split_text(text, max_length=1800):
    """改行単位で文字制限内に安全に分割するヘルパー関数"""
    lines = text.split("\n")
    chunks = []
    current_chunk = ""

    for line in lines:
        if len(current_chunk) + len(line) + 1 > max_length:
            chunks.append(current_chunk.rstrip())
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk.strip():
        chunks.append(current_chunk.rstrip())

    return chunks


def generate_and_post(prompt_text, target_ch_id, part_title, append_footer=True):
    # Gemini 3 シリーズ推奨（最新のモデル名を優先的に試行）
    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",  
        "gemini-3.1-flash-lite", 
        "gemini-3-flash",
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
                break  # 成功したら試行ループを抜ける
            except Exception as e:
                print(f"    └ 試行 {attempt}/3 失敗 ({model_name}): {e}", flush=True)
                time.sleep(5)
                
        if summary_text:
            break  # 成功したらモデル探索ループも抜ける

    if summary_text and used_model and append_footer:
        summary_text += f"\n\n*※ この要約は `{used_model}` で作成されました。*"

    if summary_text:
        chunks = safe_split_text(summary_text, max_length=1800)
        for idx, chunk in enumerate(chunks):
            requests.post(
                f"https://discord.com/api/v10/channels/{target_ch_id}/messages",
                headers=headers,
                json={"content": chunk},
            )
            time.sleep(1)

    return summary_text or ""


# =========================================================
# ★ 4. 残り日程（折り返し＆最終日前日）のリマインド自動処理（修正版）
# =========================================================
def process_reminders(info_summary_text, logs_body):
    """
    1便目の要約結果（info_summary_text）に含まれているイベント・コンテンツのみを対象にして
    リマインド判定を行います。
    """
    if not info_summary_text:
        print("  └ [リマインド確認] 1便目の要約テキストが取得できなかったためスキップします。", flush=True)
        return

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    today_date = now_jst.date()

    remind_targets = []

    print("  └ [リマインド判定ログ]", flush=True)

    for item in parsed_events_list:
        raw = item["raw_text"]
        start_dt = item["start_dt"]
        end_dt = item["end_dt"]

        if not end_dt:
            continue

        # ---------------------------------------------------------
        # ★ イベント名判定の柔軟化（AI要約による表記ゆれ対応）
        # ---------------------------------------------------------
        event_name_clean = re.sub(r"\d{1,2}/\d{1,2}.*$", "", raw).strip()
        event_keywords = [re.sub(r"[【】「」・\s]", "", k) for k in re.split(r"[\s・]", event_name_clean) if len(k) >= 2]

        is_in_summary = any(kw in info_summary_text for kw in event_keywords)

        if not is_in_summary:
            print(f"    ・[{event_name_clean[:15]}] スキップ: 1便目の要約テキスト内に該当文字列が見つかりません。", flush=True)
            continue

        remind_type = None  # '折り返し' または '最終日前日'
        
        # 日付（date）のみで判定
        end_date = end_dt.date()
        last_day_remind_date = end_date - timedelta(days=1)

        mid_date = None
        if start_dt:
            start_date = start_dt.date()
            total_days = (end_date - start_date).days

            if total_days >= 5:
                half_days = total_days // 2
                mid_date = start_date + timedelta(days=half_days)

        if last_day_remind_date == today_date:
            remind_type = "最終日前日"
        elif mid_date and mid_date == today_date:
            remind_type = "折り返し"

        mid_str = f"折り返し: {mid_date}" if mid_date else "折り返し: なし"
        last_str = f"最終日前日: {last_day_remind_date}"
        print(f"    ・[{event_name_clean[:15]}] 終了日: {end_date} | 予定 ({mid_str} / {last_str}) -> 判定: {remind_type or '対象外(本日実行なし)'}", flush=True)

        if remind_type:
            remind_targets.append(
                {
                    "raw": raw,
                    "start_dt": start_dt,
                    "end_dt": end_dt,
                    "remind_type": remind_type,
                }
            )

    if not remind_targets:
        print("  └ [リマインド確認] 本日の要約に含まれる対象イベントでリマインド該当のものがないため終了します。", flush=True)
        return

    # ---------------------------------------------------------
    # ★ リマインド文面の生成・投稿処理（チャンネル振り分け＆プロンプト厳格化）
    # ---------------------------------------------------------
    for target in remind_targets:
        raw = target["raw"]
        remind_type = target["remind_type"]

        # ★ 送信先チャンネルの厳密な判定ロジック
        if "合同火力演習" in raw:
            target_ch = "1380191122948624517"  # 合同火力演習チャンネル
            is_battle = True
        elif "制約解除決戦" in raw:
            target_ch = "1386327021704974478"  # 制約解除決戦チャンネル
            is_battle = True
        elif any(k in raw for k in ["総力戦", "大決戦"]):
            target_ch = BATTLE_CHANNEL_ID      # 総力戦・大決戦（1379058754716307516）
            is_battle = True
        else:
            target_ch = TALK_CHANNEL_ID        # ブルアカ雑談（一般イベント・キャンペーン）
            is_battle = False

        if is_battle:
            prompt_remind = f"""
あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」のバトル系専用チャンネルの先生（メンバー）に向けてリマインドを出してください。

【対象コンテンツ】
・概要：{raw}
・タイミング：{remind_type}

【参考：過去24時間のサーバー内の会話ログ】
{logs_body}

【出力・表現ルール】
・アロナとプラナの「2人の掛け合い（会話形式）」で作成してください。
・**アロナ**: 明るく元気、少し慌てん坊。
・**プラナ**: 冷静沈着、正確なデータや補足情報を添える。
・【★重要：会話ログの反映】上記の会話ログ内に【対象コンテンツ】（ボス名やスコア、編成、TL、苦戦している話など）に関する発言がある場合は、プラナまたはアロナが「サーバーの先生方は〇〇の編成やTL（タイムライン）で工夫されているようですね」「〇〇難易度に苦戦されている先生もお見かけしました」のように具体的に触れてください。会話ログに該当する話題が無い場合は無理に捏造せず、一般的な応援・アドバイスに留めてください。
・【折り返しの場合】：中盤の進行ペースやチケット消化、編成の試行錯誤を促す会話にしてください。
・【最終日前日の場合】：明日午前4:00に終了すること（チケット消失）を強く注意喚起し、スコア詰めや最終確認を促してください。
・【★重要：テンポと長さのバランス】
  - 1回の発言は【最大60〜80文字程度】に抑え、長文にならないようコンパクトにまとめてください。
  - 会話ログの盛り上がり（話題の多さや熱量）に応じて、会話の往復回数は自由に増やしてください。
    （目安：盛り上がりが少ない日＝3〜4ターン / 攻略や話題で大盛り上がり＝5〜8ターン）
  - 短いテンポのやり取り（掛け合い）を重ねることで、テンポよく読めるようにしてください。
  
【絶対遵守の制約事項】
・提供されたイベント情報・Wikiテキストに書かれている事実のみに基づいて出力してください。
・「メンテナンス」については、入力データ内に「〇月〇日 メンテ実施」等の明確な記載がない限り、絶対に「明日メンテナンスがある」「メンテナンスに注意」等の発言を含めないでください。
・イベントの種別（総力戦、大決戦、合同火力演習、制約解除決戦など）を勝手に変更・混同しないでください。

【★時間経過と返信機能（↩️）に関する判断ルール】
・ログには `[HH:MM]` の投稿時刻と、返信機能が使われた場合の `(↩️ 〇〇の「...」への返信)` が含まれます。
・時間が数時間離れていても、`(↩️ ...への返信)` がある場合は「過去の話題への反応・継続」として同じ話題として扱ってください。
・返信機能がない状態で数時間以上間隔が空いている場合は、新しい話題・単発のつぶやきとして判断してください。

【フォーマット例】
**アロナ**: 「〜〜（1言目）」
**プラナ**: 「〜〜（補足・データ）」
**アロナ**: 「〜〜（リアクション）」
**プラナ**: 「〜〜（追加情報）」
"""
            print(
                f"  └ [リマインド送信] バトル系 ({raw}): {remind_type} -> Channel: {target_ch}",
                flush=True,
            )
            generate_and_post(
                prompt_remind,
                target_ch,
                f"リマインド(バトル-{remind_type})",
                append_footer=False,
            )

        else:
            prompt_remind = f"""
あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」のブルアカ雑談チャンネルの先生（メンバー）に向けてリマインドを出してください。

【対象イベント】
・概要：{raw}
・タイミング：{remind_type}

【参考：過去24時間のサーバー内の会話ログ】
{logs_body}

【出力・表現ルール】
・アロナとプラナの「2人の掛け合い（会話形式）」で作成してください。
・**アロナ**: 明るく元気、健気。
・**プラナ**: 冷静沈着、アロナをナイスフォローする。
・【★重要：会話ログの反映】上記の会話ログ内に【対象イベント】（ストーリーの感想、チャレンジステージ、ガチャ・ピックアップ、ショップのオーパーツなど）に関する発言がある場合は、「先生方は〇〇のストーリーやチャレンジで盛り上がっていましたね！」「〇〇の交換を済ませた先生もいらっしゃるみたいです」のように具体的に触れてください。会話ログに該当する話題が無い場合は無理に捏造せず、一般的な呼びかけに留めてください。
・【折り返しの場合】：イベントストーリーの読了状況やショップ交換の進捗に触れてください。
・【最終日前日の場合】：キャンペーンやイベントの終了日時に合わせた注意喚起（やり残しチェックやAP消化など）を促してください。
・【★重要：テンポと長さのバランス】
  - 1回の発言は【最大60〜80文字程度】に抑え、長文にならないようコンパクトにまとめてください。
  - 会話ログの盛り上がり（話題の多さや熱量）に応じて、会話の往復回数は自由に増やしてください。
    （目安：盛り上がりが少ない日＝3〜4ターン / 攻略や話題で大盛り上がり＝5〜8ターン）
  - 短いテンポのやり取り（掛け合い）を重ねることで、テンポよく読めるようにしてください。

【絶対遵守の制約事項】
・提供されたイベント情報・Wikiテキストに書かれている事実のみに基づいて出力してください。
・「メンテナンス」については、入力データ内に「〇月〇日 メンテ実施」等の明確な記載がない限り、絶対に「明日メンテナンスがある」「日付が変わったら早めの消化を」等の発言を含めないでください。
・イベントの種別を勝手に変更・混同しないでください。

【★時間経過と返信機能（↩️）に関する判断ルール】
・ログには `[HH:MM]` の投稿時刻と、返信機能が使われた場合の `(↩️ 〇〇の「...」への返信)` が含まれます。
・時間が数時間離れていても、`(↩️ ...への返信)` がある場合は「過去の話題への反応・継続」として同じ話題として扱ってください。
・返信機能がない状態で数時間以上間隔が空いている場合は、新しい話題・単発のつぶやきとして判断してください。

【フォーマット例】
**アロナ**: 「〜〜（1言目）」
**プラナ**: 「〜〜（補足・データ）」
**アロナ**: 「〜〜（リアクション）」
**プラナ**: 「〜〜（追加情報）」
"""
            print(
                f"  └ [リマインド送信] イベント系 ({raw}): {remind_type} -> Channel: {target_ch}",
                flush=True,
            )
            generate_and_post(
                prompt_remind,
                target_ch,
                f"リマインド(イベント-{remind_type})",
                append_footer=False,
            )


# =========================================================
# 実行部
# =========================================================
print("[5/6] 1つ目のメッセージ（お知らせ編）を生成＆投稿中...", flush=True)
info_summary_text = generate_and_post(prompt_info, TARGET_CHANNEL_ID, "お知らせ編")

time.sleep(3)

print("[6/6] 2つ目のメッセージ（会話要約編）を生成＆投稿中...", flush=True)
generate_and_post(prompt_chat, TARGET_CHANNEL_ID, "会話要約編")

time.sleep(3)

print("[補足] 残り日程（折り返し＆最終日前日）のリマインド判定＆実行中...", flush=True)
process_reminders(info_summary_text, logs_body)
