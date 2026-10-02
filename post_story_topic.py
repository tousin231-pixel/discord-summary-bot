import os
import random
import re
from google import genai
import requests

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TARGET_CHANNEL_ID = "1376909055091671071"
FORUM_CHANNEL_ID = "1419978214394167296"

headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}


def get_target_forum_threads():
    print("[1/4] フォーラムのタグ情報を取得中...", flush=True)
    forum_res = requests.get(
        f"https://discord.com/api/v10/channels/{FORUM_CHANNEL_ID}",
        headers=headers,
        timeout=10,
    )
    if forum_res.status_code != 200:
        print(f"⚠️ フォーラム情報取得失敗: {forum_res.status_code}", flush=True)
        return []

    forum_data = forum_res.json()
    available_tags = forum_data.get("available_tags", [])

    target_tag_ids = set()
    for tag in available_tags:
        tag_name = tag.get("name", "")
        if "ブルアカ" in tag_name or "ストーリー感想" in tag_name:
            target_tag_ids.add(tag.get("id"))

    print(f"  └ 該当タグID: {target_tag_ids}", flush=True)

    if not target_tag_ids:
        print("⚠️ 対象となるタグが見つかりませんでした。", flush=True)
        return []

    guild_id = forum_data.get("guild_id")
    all_threads = []

    print("[2/4] アクティブ＆アーカイブ済みスレッドを検索中...", flush=True)

    if guild_id:
        active_res = requests.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/threads/active",
            headers=headers,
            timeout=10,
        )
        if active_res.status_code == 200:
            threads = active_res.json().get("threads", [])
            for th in threads:
                if th.get("parent_id") == FORUM_CHANNEL_ID:
                    all_threads.append(th)

    archived_res = requests.get(
        f"https://discord.com/api/v10/channels/{FORUM_CHANNEL_ID}/threads/archived/public",
        headers=headers,
        timeout=10,
    )
    if archived_res.status_code == 200:
        archived_threads = archived_res.json().get("threads", [])
        all_threads.extend(archived_threads)

    matched_threads = []
    seen_ids = set()

    for th in all_threads:
        th_id = th.get("id")
        if th_id in seen_ids:
            continue
        seen_ids.add(th_id)

        applied_tags = set(th.get("applied_tags", []))
        if applied_tags & target_tag_ids:
            matched_threads.append(th)

    print(f"  └ 条件にマッチしたスレッド数: {len(matched_threads)} 件", flush=True)
    return matched_threads


def get_thread_first_message(thread_id):
    res = requests.get(
        f"https://discord.com/api/v10/channels/{thread_id}/messages?limit=1&after=0",
        headers=headers,
        timeout=10,
    )
    if res.status_code == 200:
        msgs = res.json()
        if msgs:
            return msgs[0].get("content", "")
    return ""


def main():
    threads = get_target_forum_threads()
    if not threads:
        print("⚠️ 該当するフォーラムスレッドが見つからなかったため終了します。", flush=True)
        return

    selected_thread = random.choice(threads)
    thread_id = selected_thread["id"]
    thread_name = selected_thread.get("name", "無題のスレッド")

    print(f"[3/4] ピックアップした話題: 「{thread_name}」(<#{thread_id}>)", flush=True)

    first_msg = get_thread_first_message(thread_id)

    prompt = f"""
あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」の「ブルアカ雑談」チャンネルの先生（メンバー）に向けて、過去にフォーラムで盛り上がったストーリーや感想の話題を1つピックアップして紹介し、雑談のきっかけを作ってください。

【紹介する話題】
・スレッド名: {thread_name}
・スレッドリンク: <#{thread_id}>
・最初の投稿内容抜粋:
{first_msg[:300]}

【出力・表現ルール】
・アロナとプラナの掛け合い（会話形式）で作成してください。
・アロナ：元気いっぱいに「先生！昔こんなストーリーの話題で盛り上がっていましたよ！」と切り出す。
・プラナ：冷静にそのスレッドの内容や見どころを補足する。
・最後にメッセージ内でスレッドリンク（<#{thread_id}>）を案内し、「先生方はこのストーリーのどのシーンが好きですか？」「ぜひ当時の感想や思い出を語り合ってみてくださいね！」と雑談を促して締めてください。

【フォーマット】
📖 **【アロナの過去ログ発掘コーナー】**

**アロナ**: 「〜」
**プラナ**: 「〜」
**アロナ**: 「〜」
"""

    print("[4/4] Gemini APIによる紹介文生成中...", flush=True)
    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash", contents=prompt
        )
        if response and response.text:
            content = response.text

            res = requests.post(
                f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}/messages",
                headers=headers,
                json={"content": content},
                timeout=10,
            )
            if res.status_code in [200, 201]:
                print("🎉 正常に雑談チャンネルへ投稿完了しました！", flush=True)
            else:
                print(f"⚠ Discord投稿エラー: {res.status_code} - {res.text}", flush=True)
    except Exception as e:
        print(f"⚠️ 生成・投稿エラー: {e}", flush=True)


if __name__ == "__main__":
    main()
