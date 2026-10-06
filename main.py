import csv
from datetime import datetime, timedelta, timezone
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from bs4 import BeautifulSoup
from google import genai
import requests

# =========================================================
# 1. 設定 & 定数管理
# =========================================================

# 環境変数
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# タイムアウト設定 (接続, 読み込み)
HTTP_TIMEOUT = (6.0, 20.0)
GEMINI_TIMEOUT_SEC = 90

# 共通ヘッダー
DISCORD_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type": "application/json",
}

# Discord チャンネルID定義
TARGET_CHANNEL_ID = "1552352658445181059"  # デイリー要約
POLL_CHANNEL_ID = "1526389409841152150"    # 投票置き場
FORUM_CHANNEL_ID = "1419978214394167296"   # フォーラム親ID
TALK_CHANNEL_ID = "1376909055091671071"    # ブルアカ雑談 (リマインド用)
BATTLE_CHANNEL_ID = "1379058754716307516"  # 総力戦・バトル系 (リマインド用)

# 収集対象のカテゴリ・チャンネル定義
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

# 試行するGeminiモデルリスト
GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
]

# タイムゾーン定義
JST = timezone(timedelta(hours=9))
UTC = timezone.utc


# =========================================================
# 2. 汎用ヘルパー関数
# =========================================================

def get_now_jst():
    """現在時刻 (JST) を取得"""
    return datetime.now(JST)

def fetch_discord_api(endpoint: str, method: str = "GET", payload: dict = None):
    """Discord API呼び出しの共通化"""
    url = f"https://discord.com/api/v10/{endpoint.lstrip('/')}"
    try:
        if method.upper() == "GET":
            res = requests.get(url, headers=DISCORD_HEADERS, timeout=HTTP_TIMEOUT)
        elif method.upper() == "POST":
            res = requests.post(url, headers=DISCORD_HEADERS, json=payload, timeout=HTTP_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        if res.status_code in [200, 201]:
            return res.json()
        print(f"⚠️ Discord API エラー [{res.status_code}]: {endpoint}", flush=True)
        return None
    except Exception as e:
        print(f"⚠️ Discord API 通信例外 ({endpoint}): {e}", flush=True)
        return None

def safe_split_text(text: str, max_length: int = 1800) -> list[str]:
    """Discordの文字制限(2000文字)を超えないよう安全にテキストを分割"""
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


# =========================================================
# 3. データ収集モジュール (Wiki, CSV, Discord)
# =========================================================

def get_bluearchive_game_events():
    """Wikiから最新イベント情報を取得"""
    url = "https://bluearchive.wikiru.jp/?%E3%82%A4%E3%83%99%E3%83%B3%E3%83%88%E4%B8%80%E8%A6%A7"
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        response = requests.get(url, headers=req_headers, timeout=HTTP_TIMEOUT)
        response.encoding = response.apparent_encoding

        if response.status_code != 200:
            print(f"⚠️ Wikiステータスコード異常: {response.status_code}", flush=True)
            return "現在特別なゲーム内お知らせはありません。", []

        soup = BeautifulSoup(response.text, "html.parser")
        today_dt = get_now_jst()
        now_year = today_dt.year

        body_div = soup.find("div", id="body") or soup
        extracted_lines = []

        start_heading = None
        for h in body_div.find_all(["h2", "h3", "h4", "p", "strong"]):
            if "開催中のイベント" in h.get_text():
                start_heading = h
                break

        if start_heading:
            curr = start_heading
            while curr:
                curr = curr.find_next_sibling()
                if not curr:
                    break

                text_content = curr.get_text()
                if any(k in text_content for k in ["報酬受け取り期間", "開催予定", "過去のイベント", "終了したイベント"]):
                    break
                if curr.name in ["h2", "h3", "h4"] and any(k in text_content for k in ["予定", "過去", "終了"]):
                    break

                items = curr.find_all(["li", "tr"])
                if not items and curr.name in ["li", "tr", "p"]:
                    items = [curr]

                for item in items:
                    t = item.get_text(separator=" ", strip=True)
                    if t and ("～" in t or "~" in t):
                        extracted_lines.append(t)

        if not extracted_lines:
            for tag in body_div.find_all(["tr", "li"]):
                t = tag.get_text(separator=" ", strip=True)
                if t and ("～" in t or "~" in t):
                    if not any(k in t for k in ["過去", "終了", "2021", "2022", "2023"]):
                        extracted_lines.append(t)

        print(f"  └ [Wiki取得] 抽出行数: {len(extracted_lines)} 件", flush=True)

        events_text_list = []
        parsed_events = []
        seen = set()

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
                s_year = int(match.group(1)) if match.group(1) else now_year
                s_month = int(match.group(2)) if match.group(2) else None
                s_day = int(match.group(3)) if match.group(3) else None

                e_year = int(match.group(6)) if match.group(6) else now_year
                e_month = int(match.group(7)) if match.group(7) else None
                e_day = int(match.group(8)) if match.group(8) else None
                e_hour = int(match.group(9)) if match.group(9) else 23
                e_min = int(match.group(10)) if match.group(10) else 59

                if s_month and e_month and s_month > e_month and not match.group(6):
                    e_year = s_year + 1

                try:
                    if e_month and e_day:
                        end_dt = datetime(e_year, e_month, e_day, e_hour, e_min, tzinfo=JST)
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
                        start_dt = datetime(s_year, s_month, s_day, 11, 0, tzinfo=JST)
                except Exception as ex:
                    print(f"    ⚠️ 日付変換エラー ({line}): {ex}", flush=True)

            events_text_list.append(f"・{line} {remaining_str}".strip())
            parsed_events.append({"raw_text": line, "start_dt": start_dt, "end_dt": end_dt})

        summary_str = (
            "\n".join(events_text_list[:12])
            if events_text_list
            else "現在特別なゲーム内お知らせはありません。"
        )
        return summary_str, parsed_events

    except Exception as e:
        print(f"⚠️ Wiki取得エラー: {e}", flush=True)
        return "現在特別なゲーム内お知らせはありません。", []


def get_today_bluearchive_birthdays(csv_path="birthday.csv"):
    """CSVから本日誕生日の生徒を取得"""
    now_jst = get_now_jst()
    target_bday_str = f"{now_jst.month}/{now_jst.day}"
    print(f"[2/6] 誕生日データの確認中... (対象日付: '{target_bday_str}')", flush=True)

    birthday_students = []
    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2:
                    name, bday = row[0].strip(), row[1].strip()
                    if bday == target_bday_str:
                        birthday_students.append(name)

        if birthday_students:
            result_str = "、".join(birthday_students)
            print(f"  └ [誕生日チェック成功] 該当生徒: {result_str}", flush=True)
            return result_str
        print(f"  └ [誕生日チェック] 本日({target_bday_str})が誕生日の生徒はいません。", flush=True)
        return None

    except FileNotFoundError:
        print(f"  ⚠️ [誕生日チェックエラー] {csv_path} が見つかりません。", flush=True)
        return None
    except Exception as e:
        print(f"  ⚠️ [誕生日チェックエラー] 読み込み例外: {e}", flush=True)
        return None


def fetch_guild_events(guild_id: str, now_utc: datetime):
    """Discordサーバーのスケジュールイベントを取得"""
    if not guild_id:
        return "本日開催予定のサーバーイベントはありません。"

    data = fetch_discord_api(f"guilds/{guild_id}/scheduled-events")
    if not data:
        return "本日開催予定のサーバーイベントはありません。"

    today_jst = now_utc.astimezone(JST).date()
    event_summary = []

    for ev in data:
        status = ev.get("status")
        start_iso = ev.get("scheduled_start_time")

        if start_iso:
            utc_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            jst_dt = utc_dt.astimezone(JST)
            time_str = jst_dt.strftime("%H:%M")
            event_date_jst = jst_dt.date()
        else:
            time_str = "時間未定"
            event_date_jst = None

        if (status == 2) or (status == 1 and event_date_jst == today_jst):
            name = ev.get("name")
            event_id = ev.get("id")
            channel_id = ev.get("channel_id") # VC等の開催チャンネルIDがある場合

            # イベント直接URLを生成
            event_url = f"https://discord.com/events/{guild_id}/{event_id}"
            
            # チャンネルIDがある場合はチャンネルメンションも追加可能
            ch_mention = f" (<#{channel_id}>)" if channel_id else ""

            # 👈 や 🔗 などの絵文字を添えて導線を強調
            event_summary.append(f"・{name} (開始: {time_str} JST){ch_mention}\n    👉 イベント詳細・参加はこちら: {event_url}")

    return "\n".join(event_summary) if event_summary else "本日開催予定のサーバーイベントはありません。"


def fetch_polls(guild_id: str, yesterday_utc: datetime):
    """投票置き場チャンネルから投票とコメントを取得"""
    messages = fetch_discord_api(f"channels/{POLL_CHANNEL_ID}/messages?limit=50")
    if not messages:
        return "現在アクティブな投票はありません。", "特になし（投票のみ進行中）"

    poll_summary = []
    poll_comments = []
    poll_authors = {}

    for msg in messages:
        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
        if "poll" in msg:
            poll_data = msg["poll"]
            is_finalized = poll_data.get("results", {}).get("is_finalized", False)

            if not is_finalized or msg_time >= yesterday_utc:
                msg_id = msg["id"]
                author_id = msg.get("author", {}).get("id")
                poll_authors[msg_id] = author_id

                msg_link = f"https://discord.com/channels/{guild_id}/{POLL_CHANNEL_ID}/{msg_id}" if guild_id else ""
                question = poll_data.get("question", {}).get("text", "（無題の投票）")
                status_label = "【投票受付中】" if not is_finalized else "【締め切り済み】"
                poll_summary.append(f"・{status_label}「{question}」\n    👉 投票はこちら: {msg_link}")

    for msg in messages:
        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
        if msg_time >= yesterday_utc and not msg.get("author", {}).get("bot", False) and "poll" not in msg:
            author = msg.get("author", {}).get("username", "Unknown")
            author_id = msg.get("author", {}).get("id")
            content = msg.get("content", "")

            ref_msg = msg.get("referenced_message")
            ref_msg_id = ref_msg.get("id") if ref_msg else None

            if content:
                if ref_msg_id in poll_authors:
                    if author_id != poll_authors[ref_msg_id]:
                        poll_comments.append(f"{author}: {content}")
                else:
                    if author_id not in poll_authors.values():
                        poll_comments.append(f"{author}: {content}")

    poll_text = "\n".join(poll_summary) if poll_summary else "現在アクティブな投票はありません。"
    comments_text = "\n".join(reversed(poll_comments)) if poll_comments else "特になし（投票のみ進行中）"
    return poll_text, comments_text


def collect_chat_logs(guild_id: str, yesterday_utc: datetime) -> str:
    """通常チャンネルおよびフォーラムの過去24時間メッセージを収集"""
    collected_data = {}

    # 1. 通常チャンネル
    for cat_name, channels in CHANNELS.items():
        collected_data[cat_name] = {}
        for ch_id, ch_name in channels.items():
            messages = fetch_discord_api(f"channels/{ch_id}/messages?limit=100")
            if not messages:
                continue

            ch_msgs = []
            for msg in reversed(messages):
                msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                if msg_time >= yesterday_utc and not msg.get("author", {}).get("bot", False):
                    author = msg.get("author", {}).get("username", "Unknown")
                    content = msg.get("content", "")

                    if content:
                        msg_jst = msg_time.astimezone(JST)
                        time_str = msg_jst.strftime("%H:%M")

                        ref_info = ""
                        ref_msg = msg.get("referenced_message")
                        if ref_msg:
                            ref_author = ref_msg.get("author", {}).get("username", "Unknown")
                            ref_content = ref_msg.get("content", "")
                            ref_snippet = (ref_content[:20] + "...") if len(ref_content) > 20 else ref_content
                            ref_info = f" (↩️ {ref_author}の「{ref_snippet}」への返信)"

                        ch_msgs.append(f"[{time_str}] {author}{ref_info}: {content}")

            if ch_msgs:
                collected_data[cat_name][f"<#{ch_id}> ({ch_name})"] = ch_msgs

    # 2. フォーラムスレッド
    collected_data["フォーラム"] = {}
    if guild_id:
        threads_data = fetch_discord_api(f"guilds/{guild_id}/threads/active")
        if threads_data:
            threads = threads_data.get("threads", [])
            target_threads = [th for th in threads if th.get("parent_id") == FORUM_CHANNEL_ID]

            for th in target_threads:
                th_id = th["id"]
                th_name = th.get("name", "スレッド")
                is_new_thread = False
                create_ts_raw = th.get("create_timestamp")

                if create_ts_raw:
                    created_at = datetime.fromisoformat(create_ts_raw.replace("Z", "+00:00"))
                    if created_at >= yesterday_utc:
                        is_new_thread = True
                else:
                    try:
                        snowflake_time = ((int(th_id) >> 22) + 1420070400000) / 1000.0
                        created_at = datetime.fromtimestamp(snowflake_time, tz=timezone.utc)
                        if created_at >= yesterday_utc:
                            is_new_thread = True
                    except Exception:
                        pass

                msg_data = fetch_discord_api(f"channels/{th_id}/messages?limit=50")
                if msg_data:
                    th_msgs = []
                    for msg in reversed(msg_data):
                        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                        if msg_time >= yesterday_utc and not msg.get("author", {}).get("bot", False):
                            author = msg.get("author", {}).get("username", "Unknown")
                            content = msg.get("content", "")
                            if content:
                                th_msgs.append(f"{author}: {content}")

                    if th_msgs:
                        prefix = "🆕 " if is_new_thread else ""
                        collected_data["フォーラム"][f"{prefix}<#{th_id}> ({th_name})"] = th_msgs

    # フォーマット整形
    logs_body = ""
    for cat_name, channels in collected_data.items():
        if channels:
            logs_body += f"\n=== カテゴリ: {cat_name} ===\n"
            for ch_tag, msgs in channels.items():
                authors = set(
                    m.split("] ")[1].split(":")[0]
                    for m in msgs
                    if "] " in m and ":" in m.split("] ")[1]
                )
                logs_body += f"--- {ch_tag} (投稿数: {len(msgs)}件 / 発言者数: {len(authors)}人) ---\n"
                logs_body += "\n".join(msgs) + "\n"

    return logs_body.strip() or "過去24時間の新規投稿はありませんでした。"


# =========================================================
# 4. Gemini API 呼び出し & Discord投稿
# =========================================================

def generate_and_post(
    client: genai.Client,
    prompt_text: str,
    target_ch_id: str,
    part_title: str,
    append_footer: bool = True,
    timeout_sec: int = GEMINI_TIMEOUT_SEC,
) -> str:
    """Gemini APIで文章を生成し、Discordに投稿する"""
    summary_text = None
    used_model = None

    def call_gemini(model_name):
        return client.models.generate_content(model=model_name, contents=prompt_text)

    for model_name in GEMINI_MODELS:
        print(f"  └ [{part_title}] モデル試行中: {model_name}", flush=True)
        for attempt in range(1, 4):
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(call_gemini, model_name)
                    response = future.result(timeout=timeout_sec)

                summary_text = response.text
                used_model = model_name
                break

            except FutureTimeoutError:
                print(f"    └ ⚠ 試行 {attempt}/3 タイムアウト ({timeout_sec}秒超過) [{model_name}]", flush=True)
            except Exception as e:
                print(f"    └ 試行 {attempt}/3 失敗 ({model_name}): {e}", flush=True)
                time.sleep(3)

        if summary_text:
            break

    if not summary_text:
        print(f"❌ [{part_title}] すべてのモデル・試行で生成に失敗（またはタイムアウト）しました。", flush=True)
        return ""

    if used_model and append_footer:
        summary_text += f"\n\n*※ この要約は `{used_model}` で作成されました。*"

    # Discordへ送信
    chunks = safe_split_text(summary_text, max_length=1800)
    for chunk in chunks:
        fetch_discord_api(f"channels/{target_ch_id}/messages", method="POST", payload={"content": chunk})
        time.sleep(1)

    return summary_text


# =========================================================
# 5. リマインド判定モジュール
# =========================================================

def process_reminders(client: genai.Client, info_summary_text: str, logs_body: str, parsed_events_list: list):
    """イベント期限のリマインド判定を行い投稿"""
    if not info_summary_text:
        print("  └ [リマインド確認] 1便目の要約テキストが取得できなかったためスキップします。", flush=True)
        return

    today_date = get_now_jst().date()
    remind_targets = []

    print("  └ [リマインド判定ログ]", flush=True)

    for item in parsed_events_list:
        raw = item["raw_text"]
        start_dt = item["start_dt"]
        end_dt = item["end_dt"]

        if not end_dt:
            continue

        event_name_clean = re.sub(r"\d{1,2}/\d{1,2}.*$", "", raw).strip()
        event_keywords = [
            re.sub(r"[【】「」・\s]", "", k)
            for k in re.split(r"[\s・]", event_name_clean)
            if len(k) >= 2
        ]

        is_in_summary = any(kw in info_summary_text for kw in event_keywords)
        if not is_in_summary:
            print(f"    ・[{event_name_clean[:15]}] スキップ: 要約内に該当文字列なし", flush=True)
            continue

        remind_type = None
        end_date = end_dt.date()
        last_day_remind_date = end_date - timedelta(days=1)

        mid_date = None
        if start_dt:
            start_date = start_dt.date()
            total_days = (end_date - start_date).days
            if total_days >= 5:
                mid_date = start_date + timedelta(days=total_days // 2)

        if last_day_remind_date == today_date:
            remind_type = "最終日前日"
        elif mid_date and mid_date == today_date:
            remind_type = "折り返し"

        mid_str = f"折り返し: {mid_date}" if mid_date else "折り返し: なし"
        last_str = f"最終日前日: {last_day_remind_date}"
        print(f"    ・[{event_name_clean[:15]}] 終了日: {end_date} | 予定 ({mid_str} / {last_str}) -> 判定: {remind_type or '対象外'}", flush=True)

        if remind_type:
            remind_targets.append({
                "raw": raw,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "remind_type": remind_type,
            })

    if not remind_targets:
        print("  └ [リマインド確認] 本日対象のリマインドイベントはありません。", flush=True)
        return

    for target in remind_targets:
        raw = target["raw"]
        remind_type = target["remind_type"]

        if "合同火力演習" in raw:
            target_ch = "1380191122948624517"
            is_battle = True
        elif "制約解除決戦" in raw:
            target_ch = "1386327021704974478"
            is_battle = True
        elif any(k in raw for k in ["総力戦", "大決戦"]):
            target_ch = BATTLE_CHANNEL_ID
            is_battle = True
        else:
            target_ch = TALK_CHANNEL_ID
            is_battle = False

        prompt_remind = build_remind_prompt(raw, remind_type, logs_body, is_battle)
        category_label = "バトル" if is_battle else "イベント"
        print(f"  └ [リマインド送信] {category_label}系 ({raw}): {remind_type} -> Channel: {target_ch}", flush=True)

        generate_and_post(
            client,
            prompt_remind,
            target_ch,
            f"リマインド({category_label}-{remind_type})",
            append_footer=False,
        )


def build_remind_prompt(raw: str, remind_type: str, logs_body: str, is_battle: bool) -> str:
    """リマインド用プロンプトの構築"""
    ch_label = "バトル系専用チャンネル" if is_battle else "ブルアカ雑談チャンネル"
    
    specific_instruction = ""
    if is_battle:
        specific_instruction = """1. 【タイミング別メッセージ】
   ・「折り返し」の場合：中盤の進行ペース確認、チケット消化、編成の試行錯誤を促す展開にしてください。
   ・「最終日前日」の場合：明日午前4:00の終了（チケット消失）を強く注意喚起し、スコア詰めや最終確認を促してください。

2. 【会話ログの反映】
   ・会話ログ内に【対象コンテンツ】に関する話題（ボス名、スコア、編成、TL、苦戦している様子など）がある場合：
     「先生方は〇〇のTL（タイムライン）で工夫されているようですね」「難易度〇〇に苦戦されている先生もお見かけしました」のように具体的に触れてください。
   ・会話ログに該当する話題が無い場合：無理に捏造せず、一般的な応援・アドバイスに留めてください。"""
    else:
        specific_instruction = """1. 【タイミング別メッセージ】
   ・「折り返し」の場合：イベントストーリーの読了状況や、ショップ交換・アイテム収集の進捗に触れる展開にしてください。
   ・「最終日前日」の場合：イベント終了日時（AP消化、やり残しチェック、報酬受取など）を強く注意喚起してください。

2. 【会話ログの反映】
   ・会話ログ内に【対象イベント】に関する話題（ストーリー感想、チャレンジステージ、ガチャ・ピックアップ、ショップのオーパーツなど）がある場合：
     「先生方は〇〇のストーリーやチャレンジで盛り上がっていましたね！」「〇〇の交換を済ませた先生もいらっしゃるみたいです」のように具体的に触れてください。
   ・会話ログに該当する話題が無い場合：無理に捏造せず、一般的な応援・呼びかけに留めてください。"""

    return f"""
あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」の{ch_label}の先生（メンバー）に向けて、対象コンテンツのリマインドメッセージを作成してください。

■ 入力データ
・概要：{raw}
・タイミング：{remind_type}

■ 参照データ（過去24時間の会話ログ）
{logs_body}

■ キャラクター設定とトーン
アロナとプラナの「2人の掛け合い（会話形式）」で作成してください。
・アロナ：明るく元気で健気。
・プラナ：冷静沈着、丁寧で静かなトーン。

■ 進行・会話展開ルール
{specific_instruction}

3. 【テンポと会話量（重要）】
   ・1回の発言は【最大60〜80文字程度】に抑え、長文にならないようコンパクトにまとめてください。
   ・ログの熱量や話題の量に応じて、掛け合いのターン数を調整してください。
     - 話題が少ない日：3〜4ターン（往復）程度
     - 話題で大盛り上がりの日：5〜8ターン（往復）程度

■ 話題の判定ルール（時間・返信機能）
・ログには `[HH:MM]` の投稿時刻と、返信機能が使われた場合の `(↩️ 〇〇の「...」への返信)` が含まれます。
・時間が離れていても `(↩️ ...への返信)` がある場合は、同一の話題（継続）として扱ってください。
・返信機能がなく数時間以上空いている場合は、独立した新しい話題として判断してください。

■ 絶対遵守事項（ファクトチェック）
1. 提供された入力データ（概要・ログ）にある事実のみに基づいて出力してください。
2. 入力データ内に「メンテナンス実施」の明確な記載がない限り、絶対に「メンテナンス」という言葉や注意喚起を含めないでください。
3. イベントの種別を勝手に変更・混同しないでください。

■ 出力フォーマット
以下の形式で会話を出力してください。

**アロナ**: 「〜〜」
**プラナ**: 「〜〜」
**アロナ**: 「〜〜」
**プラナ**: 「〜〜」
"""


# =========================================================
# 6. メイン実行部
# =========================================================

def main():
    print("[1/6] 環境変数と初期設定のチェック...", flush=True)
    if not DISCORD_BOT_TOKEN or not GEMINI_API_KEY:
        print("❌ エラー: DISCORD_BOT_TOKEN または GEMINI_API_KEY が設定されていません。")
        return

    now_utc = datetime.now(UTC)
    yesterday_utc = now_utc - timedelta(days=1)

    # ギルドIDの取得
    guild_id = None
    ch_info = fetch_discord_api(f"channels/{TARGET_CHANNEL_ID}")
    if ch_info:
        guild_id = ch_info.get("guild_id")

    # データ収集
    print("[2/6] Wikiイベント情報＆誕生日情報を取得中...", flush=True)
    game_event_text, parsed_events_list = get_bluearchive_game_events()
    today_student_birthday = get_today_bluearchive_birthdays()
    birthday_info_text = f"本日お誕生日の生徒: {today_student_birthday}ちゃん" if today_student_birthday else "本日お誕生日の生徒はいません。"

    print("[3/6] 本日開催のイベント＆投票情報を取得中...", flush=True)
    event_text = fetch_guild_events(guild_id, now_utc)
    poll_text, poll_comments_text = fetch_polls(guild_id, yesterday_utc)

    print("[4/6] 過去24時間のチャットログを収集・整形中...", flush=True)
    logs_body = collect_chat_logs(guild_id, yesterday_utc)

    print("[5/6] Gemini APIによる要約を作成・投稿中...", flush=True)
    client = genai.Client(api_key=GEMINI_API_KEY)

    # プロンプト作成 (お知らせ編)
    prompt_info = f"""
あなたはDiscordサーバー「足跡の化石」の広報Botです。
以下の【入力データ】を元に、ゲーム・サーバーの連絡事項をまとめた【お知らせ編】を作成してください。

■ 入力データ
・【ブルアカ生徒の本日誕生日情報】：{birthday_info_text}
・【本日開催のディスコ―ドイベント】：{event_text}
・【投票置き場の新着投票（過去24時間）】：{poll_text}
・【ブルアカ最新ゲーム内イベント情報】：{game_event_text}

■ キャラクター設定とトーン
・ブルーアーカイブの「アロナ」として、元気で健気な言葉遣いを徹底してください。
・冒頭は朝の挨拶から始めてください。
・メッセージの最後は、お知らせ編を終えて次の「会話要約編」の投稿を楽しみに行きたくなるような、一言添えた締めくくりにしてください。（

■ 出力・表示分岐ルール
1. 【誕生日・サーバーイベント・投票情報の表示判定】
   ・3つ全てにおいて該当情報が一切ない場合：🎉 **本日の誕生日・サーバーイベント・新着投票はありません！**
   ・1つでも該当情報がある場合：該当する項目の見出しのみを立てて詳細を記載。
2. 【ゲーム情報の表示判定】
   ・該当する情報が無い場合は、見出しごと省略してください。
3. 【階層・インデントのルール】
   ・同カテゴリ内に複数の情報がある場合は、インデント（スペース2つ＋`- `）を入れてぶら下げてください。

■ 出力フォーマット
📢 **足跡の化石 デイリーサマリー（お知らせ・ゲーム情報編）**

（アロナからの朝の挨拶）

🎂 **本日の誕生日**
（情報がある場合のみ記載）

📅 **本日のサーバーイベント**
（情報がある場合のみ、JST時間およびイベントURL・リンクを添えて記載）

📊 **投票置き場のお知らせ**
（情報がある場合のみ記載）
- 現在進行中の投票テーマとURL・メッセージリンク
- 💬 **メンバーの反応**: （メンバーからのコメントや議論の様子を要約）

💙 **ブルーアーカイブ 最新ゲーム情報**
（情報がある場合のみカテゴリ形式で記載）

（ゲーム情報の振り返り ＋ 次の『会話要約編』へつなぐ切り替えの挨拶）
"""

    # プロンプト作成 (会話要約編)
    prompt_chat = f"""
あなたはDiscordサーバー「足跡の化石」の広報Botです。
以下の【会話ログ】を元に、昨日のサーバーメンバーの盛り上がりをまとめた【会話要約編】を作成してください。

■ コミュニティ前提ルール（厳格適用）
・ネタバレOK・歓迎のサーバーです。
・ゲーム内用語、ボス名、キャラ名は【会話ログに実際に存在する表記】のみを使用してください。
・カッコ書き補足（例：〇〇（△△））は、同一ログ内に公称と俗称の両方が実際に書き込まれている場合のみ許可します。

■ キャラクター設定とトーン
・ブルーアーカイブの「アロナ」として、元気で健気な言葉遣いを徹底してください。
・直前にゲーム情報の振り返り ＋ 次の『会話要約編』へつなぐ切り替えの挨拶が投稿されています。
・最後に、昨日一日を振り返るアロナからの感想＆朝の締めくくりのメッセージを添えてください。

■ 出力・フォーマットルール
・同じチャンネル（<#ID>）を要約内で2度以上登場させないでください。
・新スレッド（ログ内で 🆕 マーク付）は頭に 🆕 を付けて出力してください。
・最後に、アロナからの締めくくりの感想＆先生への労いメッセージを添えてください。

■ 単発投稿の除外・誇張防止ルール
1. 返信がなく1人の単発投稿で終わっている話題は除外してください。
2. 要約対象は「複数人がやり取りしている話題」のみです。

【出力フォーマット】
💬 **足跡の化石 デイリーサマリー（みんなの会話要約編）**

☕ **カテゴリ：シャーレ談話室**
- <#1376909055091671071>
  - 【会話ログ】に基づいた話題1

⚔️ **カテゴリ：争いの足跡**
- <#1379058754716307516>
  - 【会話ログ】に基づいた話題1

💬 **カテゴリ：フォーラム**
- <#スレッドID>
  - 【会話ログ】に基づいた話題1
---
（アロナからの締めくくりの挨拶）

【会話ログ】
{logs_body}
"""

    # 1便目・2便目の生成と送信
    info_res = generate_and_post(client, prompt_info, TARGET_CHANNEL_ID, "1便目:お知らせ")
    time.sleep(2)
    generate_and_post(client, prompt_chat, TARGET_CHANNEL_ID, "2便目:会話要約")

    # リマインドチェック
    print("[6/6] イベントリマインド判定処理を開始...", flush=True)
    process_reminders(client, info_res, logs_body, parsed_events_list)

    print("🎉 すべての処理が正常に完了しました！", flush=True)


if __name__ == "__main__":
    main()
