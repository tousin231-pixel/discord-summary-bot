import os
import discord
from google import genai
from datetime import datetime, timedelta, timezone

# 環境変数から設定値を取得
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "1376909055091671071"))

ai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")
    channel = discord_client.get_channel(CHANNEL_ID)
    if not channel:
        print("Channel not found")
        await discord_client.close()
        return

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    messages = []
    async for msg in channel.history(limit=200, after=yesterday):
        if not msg.author.bot:
            messages.append(f"{msg.author.display_name}: {msg.content}")

    messages_text = "\n".join(messages) if messages else "（過去24時間の新規メッセージはありませんでした）"

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

    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    await channel.send(response.text)
    print("Successfully sent summary!")
    await discord_client.close()

discord_client.run(DISCORD_BOT_TOKEN)
