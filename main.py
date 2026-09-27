import os
import time
import requests
from google import genai
from datetime import datetime, timedelta, timezone

print("[1/5] 環境変数とチャンネル構成の読み込み...", flush=True)
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
        "1485269967862501557": "談話室2 (VCテキスト)"
    },
    "争いの足跡": {
        "1379058754716307516": "総力戦・大決戦",
        "1380191122948624517": "合同火力演習",
        "1377257209083203614": "戦術対抗戦・編成",
        "1386327021704974478": "制約解除決戦",
        "1379072804988784780": "任務・イベント・その他",
        "1376909055091671075": "作戦室1 (VCテキスト)",
        "1527146347306680545": "作戦室2 (VCテキスト)"
    }
}

headers = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
}

now = datetime.now(timezone.utc)
yesterday = now - timedelta(days=1)

# まず guild_id (サーバーID) を取得
guild_id = None
ch_info_res = requests.get(f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}", headers=headers)
if ch_info_res.status_code == 200:
    guild_id = ch_info_res.json().get("guild_id")

print("[2/5] 本日開催のイベント＆投票情報を取得中...", flush=True)

# 1. Discordイベント情報の取得（本日開催分のみ抽出・JST表示対応）
event_summary = []
if guild_id:
    event_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events", headers=headers)
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

            is_today_event = (status == 2) or (status == 1 and event_date_jst == today_jst)

            if is_today_event:
                name = ev.get("name")
                event_summary.append(f"・{name} (開始: {time_str} JST)")

event_text = "\n".join(event_summary) if event_summary else "本日開催予定のサーバーイベントはありません。"

# 2. 投票置き場からの投票データ＆関連コメント取得（進行中の投票＋過去24時間のコメント）
poll_summary = []
poll_comments = []
poll_res = requests.get(f"https://discord.com/api/v10/channels/{POLL_CHANNEL_ID}/messages?limit=50", headers=headers)

if poll_res.status_code == 200:
    for msg in poll_res.json():
        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
        
        # A. Discord標準の投票機能 (Poll) の処理
        if "poll" in msg:
            poll_data = msg["poll"]
            # 投票がまだ終了していない（is_finalizedがFalse）、または過去24時間以内に投稿された場合
            is_finalized = poll_data.get("results", {}).get("is_finalized", False)
            
            if not is_finalized or msg_time >= yesterday:
                msg_id = msg["id"]
                msg_link = f"https://discord.com/channels/{guild_id}/{POLL_CHANNEL_ID}/{msg_id}" if guild_id else ""
                question = poll_data.get("question", {}).get("text", "（無題の投票）")
                
                status_label = "【投票受付中】" if not is_finalized else "【締め切り済み】"
                poll_summary.append(f"・{status_label}「{question}」\n  👉 投票はこちら: {msg_link}")

        # B. 投票に関するテキストコメント（過去24時間以内のBot以外の感想など）
        elif msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
            author = msg.get("author", {}).get("username", "Unknown")
            content = msg.get("content", "")
            if content:
                poll_comments.append(f"{author}: {content}")

poll_text = "\n".join(poll_summary) if poll_summary else "現在アクティブな投票はありません。"
poll_comments_text = "\n".join(reversed(poll_comments)) if poll_comments else "なし"

print("[3/5] 対象チャンネル＆フォーラムから過去24時間のメッセージを収集...", flush=True)
collected_data = {}

for cat_name, channels in CHANNELS.items():
    collected_data[cat_name] = {}
    for ch_id, ch_name in channels.items():
        res = requests.get(f"https://discord.com/api/v10/channels/{ch_id}/messages?limit=100", headers=headers)
        if res.status_code == 200:
            messages = res.json()
            ch_msgs = []
            for msg in reversed(messages):
                msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                if msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
                    author = msg.get("author", {}).get("username", "Unknown")
                    content = msg.get("content", "")
                    if content:
                        ch_msgs.append(f"{author}: {content}")
            
            if ch_msgs:
                collected_data[cat_name][f"<#{ch_id}> ({ch_name})"] = ch_msgs

# フォーラムのアクティブスレッド取得
collected_data["フォーラム"] = {}
if guild_id:
    guild_threads_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/threads/active", headers=headers)
    if guild_threads_res.status_code == 200:
        threads_data = guild_threads_res.json()
        threads = threads_data.get("threads", [])
        
        target_threads = [th for th in threads if th.get("parent_id") == FORUM_CHANNEL_ID]
        
        for th in target_threads:
            th_id = th["id"]
            th_name = th.get("name", "スレッド")
            
            msg_res = requests.get(f"https://discord.com/api/v10/channels/{th_id}/messages?limit=50", headers=headers)
            if msg_res.status_code == 200:
                th_msgs = []
                for msg in reversed(msg_res.json()):
                    msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                    if msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
                        author = msg.get("author", {}).get("username", "Unknown")
                        content = msg.get("content", "")
                        if content:
                            th_msgs.append(f"{author}: {content}")
                
                if th_msgs:
                    collected_data["フォーラム"][f"<#{th_id}> ({th_name})"] = th_msgs

# ログテキスト作成
logs_body = ""
for cat_name, channels in collected_data.items():
    if channels:
        logs_body += f"\n=== カテゴリ: {cat_name} ===\n"
        for ch_tag, msgs in channels.items():
            logs_body += f"--- {ch_tag} ---\n"
            logs_body += "\n".join(msgs) + "\n"

if not logs_body.strip():
    logs_body = "過去24時間の新規投稿はありませんでした。"

print("[4/5] Gemini APIによる要約作成中...", flush=True)
client = genai.Client(api_key=GEMINI_API_KEY)

prompt = f"""
あなたはDiscordサーバー「足跡の化石」の広報Botです。
以下のログを元に、メンバーがパッと見て要点を把握でき、サーバーの盛り上がりが伝わる簡潔なデイリー要約を作成してください。

【コミュニティ前提ルール】
・ネタバレOK・歓迎のサーバーです。ストーリー、キャラクター、編成、攻略などの具体的な内容を隠さず記載してください。
・ブルアカの用語やボス（例: 「ホバークラフト」と「ドラム缶ガニ」など）を混同・ごちゃ混ぜにせず正確に識別して要約してください。

【出力ルール】
・**同じチャンネル（<#ID>）を2度以上登場させないでください。** 1つのチャンネルで複数の話題がある場合は、インデント（下げて `- `）でぶら下げてください。
・広報Botらしくメンバーの熱量や情景が浮かぶ親しみやすい文末にしてください。
・プロンプト内の例の単語をそのまま出力せず、必ず【会話ログ】に存在する内容のみを要約してください。
・目が滑らないよう、要点だけを短く・テンポ良くまとめてください。
・通常チャンネル名・VCチャット・フォーラムスレッド名は指定された「<#ID>」のリンク表記をそのまま使用してください。
・【会話ログ】が空の場合は、各カテゴリに「本日の新規投稿はありません。」と記載してください。

【出力フォーマット例】
📢 **足跡の化石 デイリーサマリー**

📅 **本日のサーバーイベント**
（イベントがある場合のみ記載、時間をJST表記で添えてください。なければ「本日開催予定のサーバーイベントはありません。」）

📊 **投票置き場のお知らせ・話題**
（1. 現在進行中の投票がある場合は、テーマと添えられているURL・メッセージリンクを省略せずに記載してください。）
（2. 投票に関してメンバーの反応がある場合は、「💬 メンバーの反応: ○○」のように短く添えてください。なければ「新着の投票はありません。」）

☕ **カテゴリ：シャーレ談話室**
- <#1376909055091671071>
  - 【会話ログ】に基づいた話題1
  - 【会話ログ】に基づいた話題2
- <#1389948670455185439>
  - 【会話ログ】に基づいた話題
（※会話があったチャンネルのみ抽出し、カテゴリ全体で5〜8行程度でまとめる。なければ「新着の会話はありません。」）

⚔️ **カテゴリ：争いの足跡**
- <#1379058754716307516>
  - 【会話ログ】に基づいた話題
（※会話があったチャンネルのみ抽出し、カテゴリ全体で5〜8行程度でまとめる。なければ「新着の会話はありません。」）

💬 **カテゴリ：フォーラム**
- <#スレッドID>
  - 【会話ログ】に基づいた話題
  （※【会話ログ】に基づいた話題がなければ「新着の会話はありません。」）

【本日開催のディスコ―ドイベント】
{event_text}

【投票置き場の新着投票（過去24時間）】
{poll_text}

【投票置き場のコメント・やり取り】
{poll_comments_text}

【会話ログ】
{logs_body}
"""

models_to_try = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
summary_text = None
used_model = None

for model_name in models_to_try:
    print(f"  └ モデル試行中: {model_name}", flush=True)
    
    for attempt in range(1, 4):
        try:
            print(f"    ├ 試行 {attempt}/3 回目...", flush=True)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            summary_text = response.text
            used_model = model_name
            print(f"✨ 要約生成成功！ (使用モデル: {model_name})", flush=True)
            break
        except Exception as e:
            print(f"    ⚠️ {model_name} (試行 {attempt}/3) でエラーが発生しました: {e}", flush=True)
            if attempt < 3:
                print("    ⏳ 15秒待機して再試行します...", flush=True)
                time.sleep(15)
            else:
                print(f"    ❌ {model_name} は3回連続で失敗しました。次のモデルに切り替えます。", flush=True)
    
    if summary_text:
        break

# 要約末尾にモデル情報を付加（失敗時はエラーメッセージを設定）
if summary_text and used_model:
    summary_text += f"\n\n*※ この要約は `{used_model}` で作成されました。*"
elif not summary_text:
    summary_text = "⚠️ **【エラー通知】**\nGemini APIの障害または高負荷により、本日のデイリー要約の自動生成に失敗しました。"

print("[5/5] 要約用チャンネルへ投稿中...", flush=True)

# 2,000文字分割送信（Discord制限対策）
max_length = 1900
chunks = [summary_text[i:i + max_length] for i in range(0, len(summary_text), max_length)]

for idx, chunk in enumerate(chunks):
    post_data = {"content": chunk}
    post_res = requests.post(f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}/messages", headers=headers, json=post_data)

    if post_res.status_code in [200, 201]:
        print(f"✨ 要約メッセージの投稿に成功しました ({idx + 1}/{len(chunks)})", flush=True)
    else:
        print(f"❌ 投稿失敗 ({idx + 1}/{len(chunks)}): {post_res.status_code} {post_res.text}", flush=True)
    time.sleep(1)
