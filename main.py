import os
import requests
from google import genai
from datetime import datetime, timedelta, timezone

print("--- [TEST 1] 環境変数の確認 ---")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "1376909055091671071")

headers = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
}

print("--- [TEST 2] Discordメッセージの取得（直近5分間）---")
url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=50"
response = requests.get(url, headers=headers)

if response.status_code != 200:
    print(f"❌ Discord API取得エラー: HTTP {response.status_code}")
    print(f"詳細: {response.text}")
    exit(1)

raw_messages = response.json()
now = datetime.now(timezone.utc)
five_minutes_ago = now - timedelta(minutes=5)

messages = []
for msg in reversed(raw_messages):
    msg_time = datetime.fromisoformat(msg["timestamp"])
    if msg_time >= five_minutes_ago and not msg.get("author", {}).get("bot", False):
        author_name = msg.get("author", {}).get("username", "Unknown")
        content = msg.get("content", "")
        if content:
            messages.append(f"{author_name}: {content}")

print(f"➔ 直近5分間の取得件数: {len(messages)} 件")
for m in messages:
    print(f"   [ログ内容] {m}")

print("--- [TEST 3] Gemini APIへの動作テスト ---")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ログは投げずに簡単なテスト指示だけ送る
test_prompt = "「Discord Botの動作テスト成功です！本番稼働に向けて準備中です。」というメッセージを親しみやすいトーンで短く出力してください。"

ai_response = ai_client.models.generate_content(
    model="gemini-2.5-flash",
    contents=test_prompt
)
print("➔ Geminiからの返答受け取り完了")
print(f"   [生成内容]: {ai_response.text}")

print("--- [TEST 4] Discordへテスト結果を投稿 ---")
post_url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"
payload = {"content": f"🧪 **【動作テスト】**\n{ai_response.text}"}
post_response = requests.post(post_url, headers=headers, json=payload)

if post_response.status_code in [200, 201]:
    print("✨ テスト投稿が成功しました！")
else:
    print(f"❌ 投稿エラー: HTTP {post_response.status_code}, {post_response.text}")
