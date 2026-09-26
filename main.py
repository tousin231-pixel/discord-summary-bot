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

print("[2/5] 本日開催のイベント＆投票情報を取得中...")
# 1. ディスコードイベント取得
ch_res = requests.get(f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}", headers=headers)
guild_id = ch_res.json().get("guild_id") if ch_res.status_code == 200 else None

events_summary = []
if guild_id:
    events_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events", headers=headers)
    if events_res.status_code == 200:
        for ev in events_res.json():
            start_time = datetime.fromisoformat(ev["scheduled_start_time"])
            if yesterday <= start_time <= (now + timedelta(days=1)):
                events_summary.append(f"・{ev['name']} (開始: {start_time.strftime('%H:%M')} UTC)")

event_text = "\n".join(events_summary) if events_summary else "本日開催のイベントはありません。"

# 2. 投票置き場からの投票データ取得（テーマのみ）
poll_summary = []
poll_res = requests.get(f"https://discord.com/api/v10/channels/{POLL_CHANNEL_ID}/messages?limit=20", headers=headers)
if poll_res.status_code == 200:
    for msg in poll_res.json():
        msg_time = datetime.fromisoformat(msg["timestamp"])
        if msg_time >= yesterday:
            if "poll" in msg:
                question = msg["poll"].get("question", {}).get("text", "（無題の投票）")
                poll_summary.append(f"・【投票受付中】「{question}」")
            elif msg.get("content"):
                author = msg.get("author", {}).get("username", "Unknown")
                content = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
                poll_summary.append(f"・{author}: {content}")

poll_text = "\n".join(poll_summary) if poll_summary else "過去24時間以内に新しく開始された投票はありません。"

print("[3/5] 対象11チャンネル＆フォーラムから過去24時間のメッセージを収集...")
collected_data = {}

# 通常の11チャンネル収集
for category_name, ch_dict in CHANNELS.items():
    collected_data[category_name] = {}
    for ch_id, ch_name in ch_dict.items():
        raw_messages = []
        before_id = None
        
        while True:
            url = f"https://discord.com/api/v10/channels/{ch_id}/messages?limit=100"
            if before_id:
                url += f"&before={before_id}"
            
            res = requests.get(url, headers=headers)
            if res.status_code != 200:
                break
                
            batch = res.json()
            if not batch:
                break
                
            reached_yesterday = False
            for msg in batch:
                msg_time = datetime.fromisoformat(msg["timestamp"])
                if msg_time < yesterday:
                    reached_yesterday = True
                    break
                raw_messages.append(msg)
                
            if reached_yesterday or len(batch) < 100:
                break
            before_id = batch[-1]["id"]

        messages = []
        for msg in reversed(raw_messages):
            if not msg.get("author", {}).get("bot", False):
                author = msg.get("author", {}).get("username", "Unknown")
                content = msg.get("content", "")
                if content:
                    messages.append(f"{author}: {content}")
        
        if messages:
            collected_data[category_name][ch_name] = messages

# --- フォーラムチャンネルのアクティブスレッド収集 ---
collected_data["フォーラム"] = {}
forum_threads_res = requests.get(f"https://discord.com/api/v10/channels/{FORUM_CHANNEL_ID}/threads/active", headers=headers)

print(f"DEBUG: フォーラムAPIステータスコード: {forum_threads_res.status_code}")
if forum_threads_res.status_code == 200:
    threads = forum_threads_res.json().get("threads", [])
    print(f"DEBUG: 取得できたアクティブスレッド数: {len(threads)}")
    for th in threads:
        print(f"DEBUG: スレッド発見 -> ID: {th['id']}, Name: {th.get('name')}")
        th_id = th["id"]
        th_name = th.get("name", "スレッド")
        
        # 各スレッドの直近メッセージを取得
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
            
            print(f"DEBUG: スレッド [{th_name}] の過去24時間メッセージ数: {len(th_msgs)}")
            if th_msgs:
                collected_data["フォーラム"][f"スレッド: {th_name}"] = th_msgs
        else:
            print(f"DEBUG: スレッド [{th_name}] メッセージ取得エラー: {msg_res.status_code}")
else:
    print(f"DEBUG: フォーラム取得失敗 エラー内容: {forum_threads_res.text}")

# ログ構築
logs_body = ""
for cat, channels in collected_data.items():
    logs_body += f"\n=== カテゴリ: {cat} ===\n"
    if not channels:
        logs_body += "（このカテゴリでの新規会話はありませんでした）\n"
    else:
        for ch_name, msgs in channels.items():
            logs_body += f"\n--- #{ch_name} ---\n"
            logs_body += "\n".join(msgs) + "\n"

print("[4/5] Gemini APIによる要約作成中...")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

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
（イベントがある場合のみ記載、なければ「本日開催のイベントはありません」）

📊 **投票置き場のお知らせ**
（新規投票や話題がある場合のみ記載、なければ「新着の投票はありません」）

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

candidate_models = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite"
]

ai_response = None

for model_name in candidate_models:
    print(f"  └ モデル試行中: {model_name}")
    try:
        ai_response = ai_client.models.generate_content(
            model=model_name,
            contents=prompt
        )
        print(f"✨ 要約生成成功！ (使用モデル: {model_name})")
        break
    except Exception as e:
        print(f"⚠️ {model_name} でエラーが発生しました: {e}")
        print("15秒待機後に次の候補モデルへ切り替えます...")
        time.sleep(15)

if not ai_response:
    print("❌ すべての候補モデルで要約生成に失敗しました。")
    exit(1)

print("[5/5] 要約用チャンネル（デイリー要約）へ投稿中...")
post_url = f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}/messages"

text = ai_response.text
chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]

for chunk in chunks:
    requests.post(post_url, headers=headers, json={"content": chunk})
    time.sleep(1)

print("✨ デイリー要約の投稿が完了しました！")
