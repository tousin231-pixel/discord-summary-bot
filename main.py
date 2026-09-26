import os
import time
import requests
from google import genai
from datetime import datetime, timedelta, timezone

print("[1/5] 環境変数とチャンネル構成の読み込み...")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 投稿先チャンネル（デイリー要約）
TARGET_CHANNEL_ID = "1552352658445181059"
# 投票置き場チャンネル
POLL_CHANNEL_ID = "1526389409841152150"
# フォーラムチャンネル（親ID）
FORUM_CHANNEL_ID = "1419978214394167296"

# 収集対象のカテゴリ・チャンネル定義
CHANNELS = {
    "シャーレ談話室": {
        "1376909055091671071": "ブルアカ雑談",
        "1389948670455185439": "それ以外の雑談",
        "1471118534347194379": "フリースペース",
        "1379430215263981690": "スクショ・動画・ファンアート",
        "1507394652091977891": "質問・総合相談所",
        "1471401063755284480": "考察・与太話とか"
    },
    "争いの足跡": {
        "1379058754716307516": "総力戦・大決戦",
        "1380191122948624517": "合同火力演習",
        "1377257209083203614": "戦術対抗戦・編成",
        "1386327021704974478": "制約解除決戦",
        "1379072804988784780": "任務・イベント・その他"
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

print("[2/5] 本日開催のイベント＆投票情報を取得中...")

# 2. Discordイベント情報の取得（本日開催分のみ抽出・JST表示対応）
event_summary = []
if guild_id:
    event_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events", headers=headers)
    if event_res.status_code == 200:
        # 現在のJST日付（年月日）を取得
        today_jst = now.astimezone(timezone(timedelta(hours=9))).date()
        
        for ev in event_res.json():
            status = ev.get("status")
            start_iso = ev.get("scheduled_start_time")
            
            if start_iso:
                utc_dt = datetime.fromisoformat(start_iso)
                jst_dt = utc_dt.astimezone(timezone(timedelta(hours=9)))
                time_str = jst_dt.strftime("%H:%M")
                event_date_jst = jst_dt.date()
            else:
                time_str = "時間未定"
                event_date_jst = None

            # 判定: 「進行中(status=2)」または「今日開催予定(status=1 かつ 開始日が今日)」
            is_today_event = (status == 2) or (status == 1 and event_date_jst == today_jst)

            if is_today_event:
                name = ev.get("name")
                event_summary.append(f"・{name} (開始: {time_str} JST)")

event_text = "\n".join(event_summary) if event_summary else "本日開催予定のサーバーイベントはありません。"

# 2. 投票置き場からの投票データ取得（テーマ＋メッセージリンク）
poll_summary = []
poll_res = requests.get(f"https://discord.com/api/v10/channels/{POLL_CHANNEL_ID}/messages?limit=20", headers=headers)
if poll_res.status_code == 200:
    for msg in poll_res.json():
        msg_time = datetime.fromisoformat(msg["timestamp"])
        if msg_time >= yesterday:
            msg_id = msg["id"]
            msg_link = f"https://discord.com/channels/{guild_id}/{POLL_CHANNEL_ID}/{msg_id}" if guild_id else ""
            
            if "poll" in msg:
                question = msg["poll"].get("question", {}).get("text", "（無題の投票）")
                poll_summary.append(f"・【投票受付中】「{question}」\n  👉 投票はこちら: {msg_link}")
            elif msg.get("content"):
                author = msg.get("author", {}).get("username", "Unknown")
                content = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
                poll_summary.append(f"・{author}: {content}\n  👉 メッセージはこちら: {msg_link}")

poll_text = "\n".join(poll_summary) if poll_summary else "過去24時間以内に新しく開始された投票はありません。"

print("[3/5] 対象11チャンネル＆フォーラムから過去24時間のメッセージを収集...")
collected_data = {}

for cat_name, channels in CHANNELS.items():
    collected_data[cat_name] = {}
    for ch_id, ch_name in channels.items():
        res = requests.get(f"https://discord.com/api/v10/channels/{ch_id}/messages?limit=100", headers=headers)
        if res.status_code == 200:
            messages = res.json()
            ch_msgs = []
            for msg in reversed(messages):
                msg_time = datetime.fromisoformat(msg["timestamp"])
                if msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
                    author = msg.get("author", {}).get("username", "Unknown")
                    content = msg.get("content", "")
                    if content:
                        ch_msgs.append(f"{author}: {content}")
            
            if ch_msgs:
                collected_data[cat_name][ch_name] = ch_msgs

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
                    msg_time = datetime.fromisoformat(msg["timestamp"])
                    if msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
                        author = msg.get("author", {}).get("username", "Unknown")
                        content = msg.get("content", "")
                        if content:
                            th_msgs.append(f"{author}: {content}")
                
                if th_msgs:
                    collected_data["フォーラム"][f"スレッド: {th_name}"] = th_msgs

# ログテキスト作成
logs_body = ""
for cat_name, channels in collected_data.items():
    if channels:
        logs_body += f"\n=== カテゴリ: {cat_name} ===\n"
        for ch_name, msgs in channels.items():
            logs_body += f"--- #{ch_name} ---\n"
            logs_body += "\n".join(msgs) + "\n"

if not logs_body.strip():
    logs_body = "過去24時間の新規投稿はありませんでした。"

print("[4/5] Gemini APIによる要約作成中...")
client = genai.Client(api_key=GEMINI_API_KEY)

prompt = f"""
あなたはDiscordサーバー「足跡の化石」の広報Botです。
以下のログを元に、メンバーがサーバー内の出来事や盛り上がりを把握できるデイリー要約を作成してください。

【コミュニティ前提ルール】
・ネタバレOK・歓迎のサーバーです。ストーリー、キャラクター、編成、攻略などの具体的な内容を隠さず記載してください。

【本日開催のディスコ―ドイベント】
{event_text}

【投票置き場の状況（過去24時間）】
{poll_text}

【出力フォーマット例】
📢 **足跡の化石 デイリーサマリー**

📅 **本日のサーバーイベント**
（イベントがある場合のみ記載、時間をJST表記で添えてください。なければ「本日開催のイベントはありません」）

📊 **投票置き場のお知らせ**
（新規投票がある場合は、テーマと添えられているURL・メッセージリンクを省略せずに記載してください。なければ「新着の投票はありません」）

☕ **カテゴリ：シャーレ談話室**（みんなが何を楽しんでいるか）
・#ブルアカ雑談: （話題の箇条書き）
（※会話があったチャンネルのみ抽出し、カテゴリ全体で5〜8行程度でまとめる）

⚔️ **カテゴリ：争いの足跡**（攻略・編成・スコアアタックの熱量）
・#総力戦・大決戦: （話題や編成、期限間際の盛り上がり等）
（※会話があったチャンネルのみ抽出し、カテゴリ全体で5〜8行程度でまとめる）

💬 **カテゴリ：フォーラム**（議論や特命スレッドの盛り上がり）
・#スレッド名: （盛り上がっている議論や話題のまとめ）
（※動きがあったスレッドのみ抽出し、数行でまとめる）

【会話ログ】
{logs_body}
"""

# モデルのフォールバック処理
models_to_try = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
summary_text = None

for model_name in models_to_try:
    try:
        print(f"  └ モデル試行中: {model_name}")
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        summary_text = response.text
        print(f"✨ 要約生成成功！ (使用モデル: {model_name})")
        break
    except Exception as e:
        print(f"  ⚠️ {model_name} でエラーが発生しました: {e}")
        time.sleep(2)

if not summary_text:
    summary_text = "⚠️ Gemini APIの高負荷により、要約の自動生成に失敗しました。"

print("[5/5] 要約用チャンネル（デイリー要約）へ投稿中...")
post_data = {
    "content": summary_text
}
post_res = requests.post(f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}/messages", headers=headers, json=post_data)

if post_res.status_code in [200, 201]:
    print("✨ デイリー要約の投稿が完了しました！")
else:
    print(f"❌ 投稿失敗: {post_res.status_code} {post_res.text}")
