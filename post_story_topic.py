import os
import random
import re
import time
from google import genai
import requests

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TARGET_CHANNEL_ID = "1376909055091671071"
FORUM_CHANNEL_ID = "1419978214394167296"

# 利用するモデルの優先順位リスト
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",  
    "gemini-3.1-flash-lite", 
    "gemini-3-flash",
]

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
        return [], set(), {}

    forum_data = forum_res.json()
    available_tags = forum_data.get("available_tags", [])

    tag_id_to_name = {}
    story_tag_ids = set()
    ba_tag_ids = set()

    for tag in available_tags:
        t_id = tag.get("id")
        t_name = tag.get("name", "")
        tag_id_to_name[t_id] = t_name

        if "ストーリー" in t_name:
            story_tag_ids.add(t_id)
        elif "ブルアカ" in t_name:
            ba_tag_ids.add(t_id)

    target_tag_ids = story_tag_ids | ba_tag_ids
    print(f"  └ 対象タグ: {tag_id_to_name}", flush=True)

    if not target_tag_ids:
        print("⚠️ 対象となるタグが見つかりませんでした。", flush=True)
        return [], set(), {}

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
    return matched_threads, story_tag_ids, tag_id_to_name


def get_thread_recent_messages(thread_id, limit=50):
    """
    スレッド内のメッセージを取得し、時系列順（古い順）に結合して返します。
    """
    res = requests.get(
        f"https://discord.com/api/v10/channels/{thread_id}/messages?limit={limit}",
        headers=headers,
        timeout=10,
    )
    if res.status_code == 200:
        msgs = res.json()
        if msgs:
            # Discord APIは新しい順で返ってくるため、時系列順（古い→新しい）に反転させる
            msgs.reverse()
            
            combined_text = []
            for m in msgs:
                author = m.get("author", {}).get("username", "メンバー")
                content = m.get("content", "").strip()
                attachments = len(m.get("attachments", []))
                attach_info = f" [画像等{attachments}件添付]" if attachments > 0 else ""

                if content or attach_info:
                    combined_text.append(f"・{author}: {content}{attach_info}")

            return "\n".join(combined_text)
    return "（投稿内容が取得できませんでした）"


def generate_content_with_fallback(client, prompt):
    """
    各モデルで最大3回チャレンジし、駄目なら次のモデルへ切り替えます。
    """
    for model_name in FALLBACK_MODELS:
        print(f"🤖 モデル [{model_name}] で生成を試みます...", flush=True)
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt
                )
                if response and response.text:
                    print(
                        f"  └  成功！ (モデル: {model_name}, 試行回数: {attempt}回目)",
                        flush=True,
                    )
                    return response.text
            except Exception as e:
                print(
                    f"  └ ⚠️ [{model_name}] 試行 {attempt}/3 失敗: {e}",
                    flush=True,
                )
                if attempt < 3:
                    time.sleep(3)  # 3秒待ってリトライ

        print(
            f"🔄 [{model_name}] で3回失敗したため、次のモデルへ切り替えます...",
            flush=True,
        )

    return None


def main():
    threads, story_tag_ids, tag_id_to_name = get_target_forum_threads()
    if not threads:
        print("⚠️ 該当するフォーラムスレッドが見つからなかったため終了します。", flush=True)
        return

    # ランダムに1つ選定
    selected_thread = random.choice(threads)
    thread_id = selected_thread["id"]
    thread_name = selected_thread.get("name", "無題のスレッド")
    applied_tags = set(selected_thread.get("applied_tags", []))

    is_story = bool(applied_tags & story_tag_ids)

    print(
        f"[3/4] ピックアップした話題: 「{thread_name}」 (ストーリー属性: {is_story})",
        flush=True,
    )

    # ★ 1件取得（get_thread_first_message）から、最大15件を取得する関数に変更
    thread_messages = get_thread_recent_messages(thread_id, limit=15)

    # ★ タグに応じた分岐指示（ネタバレ解禁方針をここに一括統合）
    if is_story:
        corner_title = "📖 **【アロナのストーリー思い出発掘コーナー】**"
        topic_instruction = """
・【話題タイプ】：ストーリー・シナリオ感想
・【ネタバレ方針】：ネタバレ全開OK！遠慮せずにストーリーの核心、黒幕や衝撃展開、名シーン、登場生徒の熱いセリフにしっかり踏み込んで語ってください。
・【内容に応じた柔軟な対応ルール】：
  - スレッドの本文や会話に具体的にストーリーの感想や展開が書かれている場合：
    → アロナとプラナでその場面や登場生徒の熱い展開を語り合い、「先生方はあのシーンどう思いましたか？」と語り合いを促す。
  - 画像のみ・作業連絡・短文・あるいは投稿自体がまだ少ない場合：
    → 無理にストーリー内容を捏造・ごまかさず、「ここでは画像作成や共有が行われていたみたいですよ！」「まだテキストの感想が少ないスレッドなので、ぜひ先生方の熱い思い出や感想を一番乗りで書き込んでみてくださいね！」と素直に状況を伝えて書き込み・雑談を促す。
・アロナ：元気いっぱいに話題を切り出し、「まさかあの展開になるとは思いませんでしたよね！」のようにストーリーの盛り上がりポイントにはしゃぐ。
・プラナ：冷静に作中の具体的な展開や見どころ、伏線、登場生徒の活躍シーン（ネタバレ含む）を詳細に補足・深掘りする。
・締めくくり：「先生方はあのシーンの〜はどう思いましたか？」「〜の展開、本当に熱かったですよね！」と具体的・核心的な場面を挙げてストーリー雑談を促す。
"""
    else:
        corner_title = "🎉 **【アロナのプレイ＆イベント過去ログ発掘コーナー】**"
        topic_instruction = """
・【話題タイプ】：ゲームプレイ（ガチャ・攻略）またはリアルイベント・生放送・コラボ、雑談など。
・【ネタバレ方針】：ネタバレ全開OK！雑談中に出たネタバレの話題も取り扱ってください！
・【★重要：内容に応じた柔軟な対応ルール】：
  - ガチャ結果、攻略報告、イベントの思い出などがしっかり書かれている場合：
    → アロナが元気いっぱいに話題を切り出し、プラナが当時の状況や思い出（ガチャの引き、イベントの熱気など）を冷静に補足する。
  - 画像のみの投稿、短文、作業・検証ログ、あるいは投稿が少ない場合：
    → 無理に話題を作り捏造せず、「ここではガチャ結果の共有や作業が行われていたみたいです！」「当時の思い出や報告をぜひ追加で書き込んでみてくださいね！」のように状況を素直に伝えて雑談や報告を促す。
・アロナ：元気いっぱいに「先生！昔こんなイベントや話題で盛り上がっていましたよ！」と切り出す。
・プラナ：冷静にそのスレッドの話題（ガチャ・攻略報告、生放送やDJライブ・スタンプラリー等のリアルイベントの思い出など）を補足する。
・締めくくり：「先生方も当時の思い出や現地での思い出はどうでしたか？」「ぜひ振り返って語り合ってみてくださいね！」と雑談を促す。
"""

    prompt = f"""
あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」の「ブルアカ雑談」チャンネルの先生（メンバー）に向けて、過去にフォーラムで盛り上がった話題を1つピックアップして紹介し、雑談のきっかけを作ってください。

【コミュニティ前提ルール】
・ネタバレOK・歓迎のサーバーです。

【紹介する話題】
・スレッド名: {thread_name}
・スレッドリンク: <#{thread_id}>
・スレッド内の書き込みログ（時系列順）:
{thread_messages[:5000]}

【話題に応じた出力指示】
{topic_instruction}

【フォーマットルール】
・アロナとプラナの掛け合い（会話形式）で作成してください。
・タイトルは必ず「{corner_title}」から始めてください。
・メッセージの途中で必ずスレッドリンク（<#{thread_id}>）を案内してください。

【出力フォーマット】
{corner_title}

**アロナ**: 「〜」
**プラナ**: 「〜」
**アロナ**: 「〜」
"""

    print("[4/4] Gemini APIによる紹介文生成を開始します...", flush=True)
    client = genai.Client(api_key=GEMINI_API_KEY)

    # 3回挑戦＆モデル切り替え付き生成
    generated_text = generate_content_with_fallback(client, prompt)

    if generated_text:
        res = requests.post(
            f"https://discord.com/api/v10/channels/{TARGET_CHANNEL_ID}/messages",
            headers=headers,
            json={"content": generated_text},
            timeout=10,
        )
        if res.status_code in [200, 201]:
            print("🎉 正常に雑談チャンネルへ投稿完了しました！", flush=True)
        else:
            print(f"⚠️ Discord投稿エラー: {res.status_code} - {res.text}", flush=True)
    else:
        print("❌ 全てのモデルとリトライで生成に失敗しました。", flush=True)


if __name__ == "__main__":
    main()
