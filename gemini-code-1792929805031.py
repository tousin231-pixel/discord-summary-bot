import os
import re
from datetime import datetime, timedelta, timezone
from google import genai
import requests

# =========================================================
# 1. 設定 & 定数管理
# =========================================================

# 環境変数（Bot Token または ユーザーToken）
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

DISCORD_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",  # ユーザーTokenの場合は "Bot " を外してください
    "Content-Type": "application/json",
}

# =========================================================
# 2. Discord API & ログ収集関数
# =========================================================

def fetch_channel_messages_by_date(channel_id: str, start_dt_utc: datetime, end_dt_utc: datetime):
    """指定したチャンネルの特定日時範囲のメッセージを取得"""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100"
    all_messages = []
    
    # Discord APIは新しい順に返るため、遡って取得する
    before_id = None
    
    while True:
        req_url = f"{url}&before={before_id}" if before_id else url
        res = requests.get(req_url, headers=DISCORD_HEADERS)
        if res.status_code != 200:
            print(f"⚠️ エラー: メッセージ取得失敗 [{res.status_code}]")
            break
            
        messages = res.json()
        if not messages:
            break
            
        reached_end = False
        for msg in messages:
            msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
            
            # 指定期間内のみ対象
            if start_dt_utc <= msg_time <= end_dt_utc:
                all_messages.append(msg)
            elif msg_time < start_dt_utc:
                reached_end = True
                break
                
        if reached_end or len(messages) < 100:
            break
            
        before_id = messages[-1]["id"]

    # 古い順（時系列順）に並び替え
    all_messages.reverse()
    
    formatted_logs = []
    for msg in all_messages:
        if msg.get("author", {}).get("bot", False):
            continue  # Botの発言を除外する場合
            
        msg_time_jst = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00")).astimezone(JST)
        time_str = msg_time_jst.strftime("%Y-%m-%d %H:%M")
        author = msg.get("author", {}).get("username", "Unknown")
        content = msg.get("content", "")
        
        if content:
            formatted_logs.append(f"[{time_str}] {author}: {content}")
            
    return formatted_logs


# =========================================================
# 3. Gemini API 要約関数
# =========================================================

def summarize_atmosphere_for_note(client: genai.Client, raw_logs: str) -> str:
    """Note記事作成向けにコミュニティの雰囲気を抽出・要約"""
    prompt = f"""
あなたはWebメディア（Noteなど）のライターです。
以下のDiscordチャットログを読み込み、このコミュニティの「今の空気感」「話題の熱量」「主なトピック」を分かりやすくまとめた要約を作成してください。

■ 目的
・Noteの記事で「このコミュニティがどんな雰囲気で盛り上がっているか」を紹介・記録するためのベース原稿を作成する。

■ 出力フォーマット
1. 🌟 **本日の全体的な空気感・ハイライト**
   （盛り上がり度、話題の傾向、コミュニティ独特のノリなど）
2. 💬 **主な話題・トークテーマ**
   （発言ログを元に2〜4つ程度のトピックに分けて要約）
3. 語語・名言・面白やり取り（あれば）

■ チャットログ
{raw_logs}
"""
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",  # ご利用のモデル名に合わせて指定
        contents=prompt
    )
    return response.text


# =========================================================
# 4. メイン実行処理
# =========================================================

def main():
    # --- 1. 取得条件の指定 ---
    target_channel_id = "1376909055091671071"  # 取得したいDiscordチャンネルID（リンクの最後の数字）
    
    # 対象日付（例: 2026年10月24日の1日分を取得したい場合）
    target_date_str = "2026-10-24"
    
    start_dt_jst = datetime.strptime(f"{target_date_str} 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    end_dt_jst = datetime.strptime(f"{target_date_str} 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    
    start_dt_utc = start_dt_jst.astimezone(UTC)
    end_dt_utc = end_dt_jst.astimezone(UTC)

    # 保存先ディレクトリ準備
    os.makedirs("output/logs", exist_ok=True)
    os.makedirs("output/summaries", exist_ok=True)

    print(f"📥 [{target_date_str}] のログを取得中...")
    logs = fetch_channel_messages_by_date(target_channel_id, start_dt_utc, end_dt_utc)
    
    if not logs:
        print("⚠️ 指定期間のログが見つかりませんでした。")
        return

    raw_log_text = "\n".join(logs)
    
    # --- 2. ローカルに生ログを保存 ---
    log_filename = f"output/logs/discord_log_{target_channel_id}_{target_date_str}.txt"
    with open(log_filename, "w", encoding="utf-8") as f:
        f.write(raw_log_text)
    print(f"💾 生ログを保存しました: {log_filename}")

    # --- 3. Gemini APIで要約 ---
    print("🤖 Gemini APIで雰囲気を要約中...")
    client = genai.Client(api_key=GEMINI_API_KEY)
    summary_text = summarize_atmosphere_for_note(client, raw_log_text)

    # --- 4. ローカルに要約テキストを保存 ---
    summary_filename = f"output/summaries/note_summary_{target_channel_id}_{target_date_str}.md"
    with open(summary_filename, "w", encoding="utf-8") as f:
        f.write(summary_filename + "\n\n" + summary_text)
    print(f"✨ 要約結果を保存しました: {summary_filename}")


if __name__ == "__main__":
    main()