import os
import random
import time
from google import genai
import requests
import concurrent.futures
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TARGET_CHANNEL_ID = "1376909055091671071"
FORUM_CHANNEL_ID = "1419978214394167296"

# --- 調整可能なパラメータ・定数 ---
HTTP_TIMEOUT = (6.0, 20.0)
MAX_RETRIES_PER_MODEL = 3       # モデルごとの最大リトライ回数
RETRY_WAIT_SEC = 5              # 503エラー時などのリトライ待機秒数
RATE_LIMIT_WAIT_SEC = 10        # 429 RateLimit発生時の待機秒数
GEMINI_HTTP_TIMEOUT_MS = 60000  # タイムアウト設定

# 安定性の高い軽量モデル優先のリスト（503対策）
FALLBACK_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

headers = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type": "application/json",
}

# 設定定数（0.25 = 25%の確率でニュース枠。テスト時は 1.0 にすると常にニュースになります）
NEWS_PROBABILITY = 0.5


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
        if "ブルアカ" in t_name:
            ba_tag_ids.add(t_id)

    print(f"  └ 全タグ一覧: {tag_id_to_name}", flush=True)

    if not ba_tag_ids:
        print("⚠️ 「ブルアカ」に関するタグが見つかりませんでした。", flush=True)
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
        if applied_tags & ba_tag_ids:
            matched_threads.append(th)

    print(f"  └ 条件にマッチしたスレッド数: {len(matched_threads)} 件", flush=True)
    return matched_threads, story_tag_ids, tag_id_to_name


def get_thread_recent_messages(thread_id, limit=50):
    """スレッド内のメッセージを取得し、時系列順（古い順）に結合して返す"""
    res = requests.get(
        f"https://discord.com/api/v10/channels/{thread_id}/messages?limit={limit}",
        headers=headers,
        timeout=10,
    )
    if res.status_code == 200:
        msgs = res.json()
        if msgs:
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


def fetch_google_news_for_topic(query="ブルーアーカイブ"):
    """GoogleニュースのRSSから最新記事のタイトルを取得"""
    print(f"  └ [Googleニュース取得] 検索中: {query}", flush=True)
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"
    req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            xml_data = res.read()
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")

        news_items = []
        for item in items[:15]:
            title = item.find("title").text if item.find("title") is not None else ""
            pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
            if title:
                news_items.append(f"・{title} ({pub_date})")

        return "\n".join(news_items) if news_items else None
    except Exception as e:
        print(f"  └ ⚠️ Googleニュース取得失敗: {e}", flush=True)
        return None


def generate_content_with_fallback(client, prompt):
    """
    daily_summary.pyと同等の堅牢なリトライ＆モデル切り替えロジック
    """
    for model_name in FALLBACK_MODELS:
        print(f"🤖 モデル [{model_name}] で生成を試みます...", flush=True)
        
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                start = time.time()
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                
                if response and response.text:
                    elapsed = time.time() - start
                    print(f"  └ ✅ 成功！ (モデル: {model_name}, 経過時間: {elapsed:.1f}秒)", flush=True)
                    return response.text

            except Exception as e:
                err = str(e)
                if "503" in err or "UNAVAILABLE" in err or "high demand" in err:
                    if attempt < MAX_RETRIES_PER_MODEL:
                        print(f"  └ ⚠️ 503混雑エラー検知 [{model_name}] → {RETRY_WAIT_SEC}秒後にリトライ ({attempt}/{MAX_RETRIES_PER_MODEL})", flush=True)
                        time.sleep(RETRY_WAIT_SEC)
                        continue
                    else:
                        print(f"  └ ⚠️ 503混雑エラー検知 [{model_name}] → 最大リトライ回数に達したため次モデルへ切替", flush=True)
                        break
                elif "429" in err or "RESOURCE_EXHAUSTED" in err:
                    print(f"  └ ⚠️ RateLimit発生 [{model_name}] {RATE_LIMIT_WAIT_SEC}秒待機", flush=True)
                    time.sleep(RATE_LIMIT_WAIT_SEC)
                    continue
                else:
                    print(f"  └ ❌ 試行 {attempt}/{MAX_RETRIES_PER_MODEL} 失敗 ({model_name}) : {e}", flush=True)
                    time.sleep(3)
                    break

        print(f"🔄 [{model_name}] での生成に失敗したため、次のモデルへ切り替えます...", flush=True)

    return None


def main():
    threads, story_tag_ids, tag_id_to_name = get_target_forum_threads()

    # ニュース枠を引くかどうかの判定（スレッド数に左右されない固定確率）
    use_google_news = (random.random() < NEWS_PROBABILITY) or (not threads)
    news_data = None

    if use_google_news:
        print("[3/4] 🎲 抽選結果：【Googleニュース連動枠】が選ばれました！", flush=True)
        news_data = fetch_google_news_for_topic()

    # ニュース枠が選ばれた＆ニュース取得に成功した場合
    if use_google_news and news_data:
        corner_title = "📰 **【アロプラチャンネル！シャーレ外・最新トピック通信】**"
        prompt = f"""あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」のブルアカ雑談チャンネルの先生（メンバー）に向けて、現実世界で話題になっているブルーアーカイブの最新ニュース（リアルイベント、コラボ、グッズ、メディア展開など）を1つピックアップして紹介し、雑談のきっかけを作ってください。

■ 入力データ
・コーナータイトル：{corner_title}
・Googleニュース最新トピック一覧：
{news_data}

■ キャラクター設定とトーン
アロナとプラナの「2人の掛け合い（会話形式）」で作成してください。
・アロナ：明るく元気で健気。現実世界のイベントやコラボにワクワクしている。
・プラナ：冷静沈着、丁寧で静かなトーン。ニュースの要点を落ち着いて補足する。

■ 進行・会話展開ルール
1. 【タイトルの案内】
   ・出力の先頭は必ず「{corner_title}」から始めてください。
2. 【トピックの選定と紹介】
   ・上記のニュース一覧から、先生たちが盛り上がりそうな明るい話題（リアルイベント、コラボカフェ、ポップアップショップ、ライブ、新グッズなど）を**1つだけ**選んで話題にしてください。
   ・複数詰め込まず、1つの話題を掘り下げてください。
3. 【雑談への誘導】
   ・締めくくりは「先生方はこのイベントやコラボ、チェックしましたか？」「現地に行く予定の先生はいますか？」など、雑談チャンネルで感想や予定を語り合いたくなる一言で締めてください。
4. 【テンポと会話量（重要）】
   ・1回の発言は【最大60〜80文字程度】に抑え、4〜6ターン（往復）程度でテンポよくまとめてください。

■ 絶対遵守事項（ファクトチェック）
・提供されたニュース見出しにある事実のみに基づいて話し、開催日程や場所などを勝手に捏造しないでください。

■ 出力フォーマット
{corner_title}

**อロナ**: 「〜〜」
**プラナ**: 「〜〜」
**アロナ**: 「〜〜」
**プラナ**: 「〜〜」
"""
    else:
        if not threads:
            print("⚠️ 該当するフォーラムスレッドが見つからず、ニュースも取得できなかったため終了します。", flush=True)
            return

        selected_thread = random.choice(threads)
        thread_id = selected_thread["id"]
        thread_name = selected_thread.get("name", "無題のスレッド")
        applied_tags = set(selected_thread.get("applied_tags", []))

        is_story = bool(applied_tags & story_tag_ids)

        print(
            f"[3/4] ピックアップした話題: 「{thread_name}」 (ストーリー属性: {is_story})",
            flush=True,
        )

        thread_messages = get_thread_recent_messages(thread_id, limit=50)

        if is_story:
            corner_title = "📖 **【アロプラチャンネル！ストーリー思い出発掘コーナー】**"
            topic_specific_rule = """・【話題タイプ】：ストーリー・シナリオ感想
・【ネタバレ方針】：ネタバレ全開OK！ストーリーの核心、黒幕や衝撃展開、名シーン、登場生徒の熱いセリフにしっかり踏み込んで語ってください。
・【会話展開の指針】：
  - アロナ：話題を切り出し、「まさかあの展開になるとは思いませんでしたよね！」のように熱く盛り上がる。
  - プラナ：作中の具体的展開や見どころ、伏線、生徒の活躍シーン（ネタバレ含む）を詳細に補足・深掘りする。
  - 締めくくり：「先生方はあのシーンの〜はどう思いましたか？」など具体的・核心的な場面を挙げてストーリー雑談を促す。"""
        else:
            corner_title = "🎉 **【アロプラチャンネル！プレイ＆イベント過去ログ発掘コーナー】**"
            topic_specific_rule = """・【話題タイプ】：ゲームプレイ（ガチャ・攻略）、リアルイベント、コラボ、雑談など
・【ネタバレ方針】：ネタバレ全開OK！雑談中に出たネタバレの話題も取り扱ってください。
・【会話展開の指針】：
  - アロナ：「先生！昔こんなイベントや話題で盛り上がっていましたよ！」と元気に切り出す。
  - プラナ：そのスレッドの話題（ガチャ・攻略報告、生放送やDJライブ・スタンプラリー等の思い出など）を冷静に補足する。
  - 締めくくり：「先生方も当時の思い出や現地での思い出はどうでしたか？」と過去ログへの書き込み・雑談を促す。"""

        prompt = f"""あなたは「ブルーアーカイブ」のアロナとプラナです。
Discordサーバー「足跡の化石」のブルアカ雑談チャンネルの先生（メンバー）に向けて、過去にフォーラムで盛り上がった話題を1つピックアップして紹介し、雑談のきっかけを作ってください。

■ 入力データ
・コーナータイトル：{corner_title}
・スレッド名: {thread_name}
・スレッドリンク: <#{thread_id}>
・スレッド内の書き込みログ（時系列順）:
{thread_messages[:5000]}

■ キャラクター設定とトーン
アロナとプラナの「2人の掛け合い（会話形式）」で作成してください。
・アロナ：明るく元気で健気。
・プラナ：冷静沈着、丁寧で静かなトーン。アロナをナイスフォローする。

■ カテゴリ別ルール
{topic_specific_rule}

■ 進行・会話展開ルール
1. 【タイトルとリンクの案内】
   ・出力の先頭は必ず「{corner_title}」から始めてください。
   ・会話の途中で、必ずスレッドリンク（<#{thread_id}>）へ導線案内を入れてください。

2. 【ログ内容に応じた柔軟な対応】
   ・感想や報告がしっかり書かれている場合：
     当時の状況や思い出（ガチャ結果、攻略、熱い展開など）を具体的に取り上げて深掘りしてください。
   ・画像のみ・短文・作業ログ、または投稿が少ない場合：
     無理に話題を捏造せず、「ここでは画像共有や作業が行われていたみたいですよ！」「まだ感想が少ないので、ぜひ先生方の思い出を一番乗りで書き込んでみてくださいね！」のように素直に状況を伝えて書き込みを促してください。

3. 【テンポと会話量（重要）】
   ・1回の発言は【最大60〜80文字程度】に抑え、長文にならないようコンパクトにまとめてください。
   ・ログの密度（話題の多さや熱量）に応じてターン数を調整してください。
     - 情報量が少ない・画像メイン：3ターン程度（アロナ→プラナ→アロナ等）でサクッと案内
     - 感想や討論で大盛り上がり：5〜8ターン程度かけ、具体的に語り合う

■ 絶対遵守事項（ファクトチェック）
・ログに書かれている事実や指定されたスレッド情報のみに基づいて会話を作成してください。
・コミュニティルールとして「ネタバレ歓迎」のため、ネタバレを理由に配慮したり言葉を濁したりする必要はありません。

■ 出力フォーマット
以下の形式で会話を出力してください。

{corner_title}

**アロナ**: 「〜〜」
**プラナ**: 「〜〜」
**アロナ**: 「〜〜」
**プラナ**: 「〜〜」
"""

    print(f"プロンプト文字数: {len(prompt)}文字", flush=True)
    print("[4/4] Gemini APIによる紹介文生成を開始します...", flush=True)
    # client生成時に http_options を渡す
    client = genai.Client(api_key=GEMINI_API_KEY,http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS})

    # 堅牢なフォールバック付き関数で生成
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
