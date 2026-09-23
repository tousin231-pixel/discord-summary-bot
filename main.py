import os
import requests
from google import genai
from datetime import datetime, timedelta, timezone

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "1376909055091671071")

# 1. Discord REST APIを使って過去24時間のメッセージを取得
headers = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
}
url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=100"

response = requests.get(url, headers=headers)
if response.status_code != 200:
    print(f"Error fetching messages: {response.status_code}, {response.text}")
    exit(1)

raw_messages = response.json()

# 過去24時間以内のメッセージのみを抽出
now = datetime.now(timezone.utc)
yesterday = now - timedelta(days=1)

messages = []
for msg in reversed(raw_messages):
    msg_time = datetime.fromisoformat(msg["timestamp"])
    if msg_time >= yesterday and not msg.get("author", {}).get("bot", False):
        author_name = msg.get("author", {}).get("username", "Unknown")
        content = msg.get("content", "")
        if content:
            messages.append(f"{author_name}: {content}")

messages_text = "\n".join(messages) if messages else "（過去24時間の新規メッセージはありませんでした）"

# 2. Gemini APIで要約を作成
ai_client = genai.Client(api_key=GEMINI_API_KEY)

prompt = f"""
以下のDiscord「ブルアカ雑談！」チャンネルの会話ログを元に、サーバーメンバー向けのデイリートピックを作成してください。

【コミュニティの前提条件】
・サーバー名: 「足跡の化石」
・特徴: ネタバレOK・歓迎のコミュニティです。
・ブルーアーカイブのストーリー、キャラ、装備や育成、ガチャなどの話題について、ネタバレや具体的な攻略・仕様も隠さず要約してください。

【過去24時間の会話ログ】
{messages_text}

【出力フォーマット】
1. 🌟 **ブルアカ雑談！ 今日のトピック（概略）**
2. 💬 **盛り上がった話題**
"""

ai_response = ai_client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt
)

# 3. Discordに要約を送信
post_url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"
payload = {
    "content": ai_response.text
}
post_response = requests.post(post_url, headers=headers, json=payload)

if post_response.status_code in [200, 201]:
    print("Successfully sent summary!")
else:
    print(f"Error posting summary: {post_response.status_code}, {post_response.text}")
