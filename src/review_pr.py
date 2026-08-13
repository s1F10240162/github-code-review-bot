import os
import re
import sys
import json
import requests
from openai import OpenAI

MAX_DIFF_LENGTH = 20000  # トークン超過を防ぐための文字数上限
MAX_TREE_ENTRIES = 300   # リポジトリ構成として送るファイル数の上限
MAX_TREE_LENGTH = 4000   # リポジトリ構成として送る文字数の上限
IGNORE_TREE_PREFIXES = (".git/", "node_modules/", "dist/", "build/", "__pycache__/", ".venv/", "venv/")

SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}
SEVERITY_LABEL = {"high": "優先度: 高", "medium": "優先度: 中", "low": "優先度: 低"}
BOT_LOGIN = "github-actions[bot]"

def get_env_variable(var_name, default=None, required=True):
    val = os.getenv(var_name, default)
    if required and not val:
        print(f"Error: 必須環境変数 {var_name} が設定されていません。")
        sys.exit(1)
    return val

def get_pr_details(repo, pr_number, token):
    """PRのタイトル・説明文とDiff（差分）を取得します。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # 差分の取得
    diff_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    response = requests.get(diff_url, headers=headers)
    if response.status_code != 200:
        print(f"Error: PR差分の取得に失敗しました (Status: {response.status_code}): {response.text}")
        sys.exit(1)
    diff_content = response.text

    # メタデータ (タイトルや概要) の取得
    meta_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    meta_response = requests.get(diff_url, headers=meta_headers)
    title, body, head_sha = "", "", ""
    if meta_response.status_code == 200:
        pr_data = meta_response.json()
        title = pr_data.get("title", "")
        body = pr_data.get("body", "") or "説明なし"
        head_sha = pr_data.get("head", {}).get("sha", "")

    return title, body, diff_content, head_sha

def get_repo_tree(repo, token, ref):
    """レビュー時の参考情報として、リポジトリのディレクトリ構成（ファイルパス一覧）を取得します。"""
    if not ref:
        return ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    tree_url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
    response = requests.get(tree_url, headers=headers)
    if response.status_code != 200:
        print(f"Warning: リポジトリ構成の取得に失敗しました (Status: {response.status_code})。構成情報なしでレビューを続行します。")
        return ""

    tree_data = response.json()
    all_paths = [
        item["path"] for item in tree_data.get("tree", [])
        if item.get("type") == "blob" and not item["path"].startswith(IGNORE_TREE_PREFIXES)
    ]
    all_paths.sort()

    paths = all_paths[:MAX_TREE_ENTRIES]
    if len(all_paths) > MAX_TREE_ENTRIES:
        paths.append(f"... 他 {len(all_paths) - MAX_TREE_ENTRIES} 件省略")

    tree_text = "\n".join(paths)
    if len(tree_text) > MAX_TREE_LENGTH:
        tree_text = tree_text[:MAX_TREE_LENGTH] + "\n... (構成情報が長すぎるため一部省略)"
    return tree_text

def analyze_diff(diff_content):
    """
    Diffを解析し、
    (1) インラインコメント可能な (ファイルパス, 新ファイル側の行番号) の集合
    (2) 行番号付きでAIに提示するための整形済みテキスト
    を作成します。行番号は新ファイル側 (追加行・変更なしの周辺行) のみ付与され、
    削除された行にはコメントできないため番号を付けません。
    """
    is_truncated = len(diff_content) > MAX_DIFF_LENGTH
    if is_truncated:
        diff_content = diff_content[:MAX_DIFF_LENGTH]

    file_line_map = {}
    annotated_blocks = []

    current_file = None
    current_lines = {}
    annotated_lines = []
    new_line_no = None

    def flush_file():
        if current_file and annotated_lines:
            file_line_map[current_file] = current_lines
            annotated_blocks.append(f"■ ファイル: {current_file}\n" + "\n".join(annotated_lines))

    for raw_line in diff_content.splitlines():
        if raw_line.startswith("diff --git "):
            flush_file()
            current_file, current_lines, annotated_lines, new_line_no = None, {}, [], None
        elif raw_line.startswith("+++ "):
            path = raw_line[4:].strip()
            current_file = None if path == "/dev/null" else re.sub(r"^[ab]/", "", path)
        elif raw_line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
            if match:
                new_line_no = int(match.group(1))
            if current_file:
                annotated_lines.append(raw_line)
        elif current_file is not None and new_line_no is not None:
            if raw_line.startswith("+"):
                current_lines[new_line_no] = raw_line[1:]
                annotated_lines.append(f"{new_line_no:>5}+ {raw_line[1:]}")
                new_line_no += 1
            elif raw_line.startswith("-"):
                annotated_lines.append(f"      - {raw_line[1:]}")
            elif raw_line.startswith(" "):
                current_lines[new_line_no] = raw_line[1:]
                annotated_lines.append(f"{new_line_no:>5}  {raw_line[1:]}")
                new_line_no += 1
            else:
                annotated_lines.append(raw_line)

    flush_file()
    return file_line_map, "\n\n".join(annotated_blocks), is_truncated

def generate_review(title, pr_body, annotated_diff, repo_tree, model_name, api_key, base_url=None):
    """OpenAI API（または大学等のプロキシAPI）を呼び出し、指摘事項をJSON構造で取得します。"""
    if base_url:
        print(f"カスタムBase URLを使用中: {base_url}")
        client = OpenAI(api_key=api_key, base_url=base_url)
    else:
        client = OpenAI(api_key=api_key)

    system_prompt = """あなたは経験豊富なシニアソフトウェアエンジニア兼コードレビュアーです。
提出されたPull Requestの変更を、リポジトリ全体のディレクトリ構成やPRの説明（意図）と照らし合わせながら分析し、必ず下記のJSON形式のみで日本語のレビュー結果を出力してください。前後に説明文やコードフェンスは付けないでください。

【入力形式】
「変更差分」はファイルごとに、行番号付きで渡されます。行番号があるのは追加行・変更なしの周辺行のみで、削除された行(行頭が `-`)には行番号がなくコメントできません。

【出力JSONスキーマ】
{
  "summary": "変更概要のサマリー (文字列)",
  "good_points": ["良かった点", ...],
  "findings": [
    {
      "file": "変更差分に登場したファイルパスと完全一致させる",
      "line": 対象行番号(変更差分に付与された番号と完全一致する整数。削除行や存在しない行は指定不可),
      "severity": "high" または "medium" または "low",
      "title": "指摘の短い見出し",
      "body": "具体的な指摘内容。必要ならMarkdownのコードブロックで修正案を含める。"
    }
  ],
  "needs_clarification": "PRの説明が薄く意図が読み取れない場合にその旨を書く文字列。問題なければ空文字列。"
}

【レビュー方針】
- 「リポジトリ構成」を参考に、変更が既存の設計・ファイル配置・命名規則と整合しているか確認してください。
- API等の公開インターフェースの定義（例: FastAPIやFlaskのエンドポイント定義、リクエスト/レスポンスに使われるスキーマ・型定義クラス、GraphQLのスキーマなど）で、フィールドの追加・削除・名称変更・型変更・必須/任意の変更を検知した場合は、それを「APIの契約変更」とみなしてください。**このルールは、HTTPエンドポイントのハンドラ関数（`@app.get`/`@app.post`/`@app.route`等のデコレータが付いた関数）や、リクエスト/レスポンスのスキーマクラス（Pydanticの`BaseModel`を継承したクラス、GraphQLの型定義など）自体の変更にのみ適用してください。それらのデコレータやスキーマ定義を伴わない、単なる内部処理用の関数・ヘルパー関数の追加や引数変更は対象外です（外部から呼び出されるインターフェースではないため）。** 該当する場合は、変更前後のフィールド構成の差分を具体的に明記し（同じ差分内でフィールドの削除と追加が両方見られる場合はリネームの可能性にも触れてください）、severity: "high"とした上で「この変更を利用している側（フロントエンド等の呼び出しコードや型定義）も合わせて更新されているか確認してください」という趣旨の一言を添えてください。ただし、このPRの差分だけでは呼び出し側の実装は見えないため、断定的に「壊れている」と決めつけず、確認を促す書き方にしてください。
- バグの可能性・セキュリティ上の脆弱性・重大なパフォーマンス問題は severity: "high"。
- 可読性・保守性の低下、例外処理の不足、エッジケースの考慮漏れ、構成との不整合は severity: "medium"。
- 軽微なリファクタリング提案、命名の改善、タイポは severity: "low"。
- 特定の行に紐づかない、ファイル全体・設計全体への指摘は findings に入れず summary に書いてください。
- 大きな問題がなければ findings は空配列にし、summary で「問題なし」と評価してリリースを後押ししてください。
- 建設的で丁寧なトーンで記述してください。
"""

    repo_tree_section = f"""
■ リポジトリ構成 (参考・一部のみの場合あり):
```
{repo_tree}
```
""" if repo_tree else ""

    user_prompt = f"""以下はレビュー対象のPull Request情報です。JSON形式で回答してください。

■ PRタイトル: {title}
■ PR概要:
{pr_body}
{repo_tree_section}
■ 変更差分 (ファイルごとに行番号付き。番号がある行のみコメント可能):
{annotated_diff}
"""

    print(f"モデル '{model_name}' を使用してレビューを生成中...")

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        raw_text = response.choices[0].message.content
    except Exception as e:
        print(f"Error: OpenAI API呼び出しに失敗しました: {e}")
        sys.exit(1)

    try:
        return json.loads(raw_text), None
    except (json.JSONDecodeError, TypeError):
        print("Warning: レビュー結果をJSONとして解析できませんでした。通常コメントにフォールバックします。")
        return None, raw_text

def build_review_comments(review_data, file_line_map):
    """AIのJSON出力を、レビュー本文(body)とインラインコメント配列(comments)に変換します。"""
    summary = review_data.get("summary") or "（サマリーがありません）"
    good_points = review_data.get("good_points") or []
    findings = review_data.get("findings") or []
    needs_clarification = review_data.get("needs_clarification") or ""

    body_parts = [f"### 📝 変更概要のサマリー\n{summary}"]
    if good_points:
        body_parts.append("### 🌟 良かった点\n" + "\n".join(f"- {p}" for p in good_points))
    if needs_clarification:
        body_parts.append(f"### ⚠️ 意図の確認\n{needs_clarification}")

    comments = []
    skipped = []
    for finding in findings:
        file_path = finding.get("file")
        try:
            line = int(finding.get("line"))
        except (TypeError, ValueError):
            line = None
        severity = finding.get("severity", "low")
        title = finding.get("title", "指摘事項")
        detail = finding.get("body", "")
        icon = SEVERITY_ICON.get(severity, "🔵")
        label = SEVERITY_LABEL.get(severity, "優先度: 低")
        comment_body = f"{icon} **【{label}】{title}**\n\n{detail}"

        if file_path in file_line_map and line in file_line_map.get(file_path, {}):
            comments.append({"path": file_path, "line": line, "side": "RIGHT", "body": comment_body})
        else:
            skipped.append((file_path, line, comment_body))

    if skipped:
        skipped_text = "\n\n".join(
            f"- `{f or '不明なファイル'}`" + (f" (行 {l})" if l else "") + f"\n  {b}"
            for f, l, b in skipped
        )
        body_parts.append(f"### 📌 その他の指摘（該当行を特定できなかったもの）\n{skipped_text}")

    return "\n\n".join(body_parts), comments

def post_review(repo, pr_number, token, commit_id, body, comments):
    """PRに、行ごとのインラインコメントを含むレビューを投稿します。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    review_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    payload = {
        "commit_id": commit_id,
        "body": f"🤖 **AI Code Review (OpenAI)**\n\n{body}",
        "event": "COMMENT",
        "comments": comments
    }

    response = requests.post(review_url, headers=headers, data=json.dumps(payload))
    if response.status_code not in (200, 201):
        print(f"Error: レビューの投稿に失敗しました (Status: {response.status_code}): {response.text}")
        sys.exit(1)

    print(f"Success: PR #{pr_number} に指摘 {len(comments)} 件を含むレビューを投稿しました！")

def post_issue_comment(repo, pr_number, token, comment_body):
    """(フォールバック用) PRに通常のコメントを1件投稿します。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    comment_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"

    payload = {
        "body": f"🤖 **AI Code Review (OpenAI)**\n\n{comment_body}"
    }

    response = requests.post(comment_url, headers=headers, data=json.dumps(payload))
    if response.status_code not in (200, 201):
        print(f"Error: PRへのコメント投稿に失敗しました (Status: {response.status_code}): {response.text}")
        sys.exit(1)

    print(f"Success: PR #{pr_number} にコードレビューを正常に投稿しました！")

def get_all_review_comments(repo, pr_number, token):
    """PRに付いている全てのインラインレビューコメント（返信含む）を取得します。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    comments = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
        response = requests.get(url, headers=headers, params={"per_page": 100, "page": page})
        if response.status_code != 200:
            print(f"Warning: レビューコメントの取得に失敗しました (Status: {response.status_code})。")
            break
        batch = response.json()
        if not batch:
            break
        comments.extend(batch)
        page += 1
    return comments

def build_thread_context(all_comments, root_id):
    """指定されたスレッド(root_idへの返信一式)を、時系列順の会話テキストに整形します。"""
    root_comment = next((c for c in all_comments if c.get("id") == root_id), None)
    if root_comment is None:
        return None, ""

    thread = [root_comment] + [
        c for c in all_comments
        if c.get("in_reply_to_id") == root_id and c.get("id") != root_id
    ]
    thread.sort(key=lambda c: c.get("created_at", ""))

    lines = []
    for c in thread:
        speaker = "🤖 AIレビュアー" if c.get("user", {}).get("login") == BOT_LOGIN else f"👤 {c.get('user', {}).get('login', '不明')}"
        lines.append(f"[{speaker}]\n{c.get('body', '')}")
    return root_comment, "\n\n".join(lines)

def generate_reply(root_comment, thread_text, latest_reply_body, model_name, api_key, base_url=None):
    """指摘へのスレッド内での返信を読み、AIとしての応答を1件生成します（自由記述、JSON化しない）。"""
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    system_prompt = """あなたは経験豊富なシニアソフトウェアエンジニア兼コードレビュアーです。
以前あなたが投稿したコードレビューの指摘に対して、PRの作成者や他の開発者から返信が届きました。
スレッドの会話履歴を踏まえ、その返信に対する応答を1件、日本語の自然な文章で簡潔に返してください。
JSON化やMarkdownの見出しは不要です。相手の発言に直接答える、短い会話文として書いてください。

【応答方針】
- 相手の説明が妥当で、指摘への対応が不要と判断できる場合は、素直にそれを認めてください。
- まだ懸念が残る場合は、何が引っかかっているのかを具体的かつ簡潔に説明してください。
- 元の指摘をそのまま繰り返さないでください。会話の続きとして自然に応答してください。
- 過度に長くならないよう、2〜4文程度を目安にしてください。
"""

    user_prompt = f"""■ 対象ファイル: {root_comment.get("path")} (行 {root_comment.get("line") or root_comment.get("original_line")})
■ 該当コードの差分:
{root_comment.get("diff_hunk", "")}

■ これまでの会話:
{thread_text}

■ 直近の返信（これに応答してください）:
{latest_reply_body}
"""

    print(f"モデル '{model_name}' を使用して返信を生成中...")
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error: OpenAI API呼び出しに失敗しました: {e}")
        sys.exit(1)

def post_reply(repo, pr_number, comment_id, token, body):
    """既存のレビューコメントスレッドに返信を投稿します。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies"
    payload = {"body": f"🤖 {body}"}

    response = requests.post(url, headers=headers, data=json.dumps(payload))
    if response.status_code not in (200, 201):
        print(f"Error: 返信の投稿に失敗しました (Status: {response.status_code}): {response.text}")
        sys.exit(1)

    print(f"Success: PR #{pr_number} のスレッドに返信しました。")

def handle_review_comment_reply(github_repository, github_token, openai_api_key, openai_base_url, model_name):
    """pull_request_review_commentイベント(誰かがコメントに返信した)を処理します。"""
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        print("Error: イベント情報(GITHUB_EVENT_PATH)が見つかりませんでした。")
        sys.exit(1)

    with open(event_path, "r", encoding="utf-8") as f:
        event_data = json.load(f)

    comment = event_data.get("comment", {})
    pr_number = event_data.get("pull_request", {}).get("number")

    if comment.get("user", {}).get("login") == BOT_LOGIN:
        print("Bot自身のコメントのため、返信をスキップします（無限ループ防止）。")
        return

    in_reply_to_id = comment.get("in_reply_to_id")
    if not in_reply_to_id:
        print("既存スレッドへの返信ではない（新規コメント）ため、スキップします。")
        return

    all_comments = get_all_review_comments(github_repository, pr_number, github_token)
    root_comment, thread_text = build_thread_context(all_comments, in_reply_to_id)

    if root_comment is None or root_comment.get("user", {}).get("login") != BOT_LOGIN:
        print("Botが開始したスレッドではないため、返信をスキップします。")
        return

    reply_text = generate_reply(
        root_comment, thread_text, comment.get("body", ""), model_name, openai_api_key, openai_base_url
    )
    post_reply(github_repository, pr_number, comment["id"], github_token, reply_text)

def main():
    openai_api_key = get_env_variable("OPENAI_API_KEY")
    openai_base_url = os.getenv("OPENAI_BASE_URL") or "https://api.openai.iniad.org/api/v1"
    github_token = get_env_variable("GITHUB_TOKEN")
    github_repository = get_env_variable("GITHUB_REPOSITORY")
    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if os.getenv("GITHUB_EVENT_NAME") == "pull_request_review_comment":
        handle_review_comment_reply(github_repository, github_token, openai_api_key, openai_base_url, model_name)
        return

    # GITHUB_EVENT_PATH から PR番号の取得を試みる (Actions環境)
    pr_number = os.getenv("PR_NUMBER")
    if not pr_number:
        event_path = os.getenv("GITHUB_EVENT_PATH")
        if event_path and os.path.exists(event_path):
            with open(event_path, "r", encoding="utf-8") as f:
                event_data = json.load(f)
                if "pull_request" in event_data:
                    pr_number = str(event_data["pull_request"]["number"])

    if not pr_number:
        print("Error: PR番号(PR_NUMBER)が見つかりませんでした。")
        sys.exit(1)

    print(f"--- AI Code Review 開始 ---")
    print(f"リポジトリ: {github_repository}")
    print(f"PR番号: #{pr_number}")
    print(f"使用モデル: {model_name}")
    print(f"Base URL: {openai_base_url}")

    title, pr_body, diff_content, head_sha = get_pr_details(github_repository, pr_number, github_token)

    if not diff_content.strip():
        print("差分が見つからないため、レビューをスキップします。")
        sys.exit(0)

    repo_tree = get_repo_tree(github_repository, github_token, head_sha)
    file_line_map, annotated_diff, is_truncated = analyze_diff(diff_content)

    review_data, raw_fallback_text = generate_review(
        title, pr_body, annotated_diff, repo_tree, model_name, openai_api_key, openai_base_url
    )

    if review_data is None:
        post_issue_comment(github_repository, pr_number, github_token, raw_fallback_text or "レビュー結果の取得に失敗しました。")
        return

    body, comments = build_review_comments(review_data, file_line_map)
    if is_truncated:
        body += "\n\n> ⚠️ **注意**: 差分量が多いため、一部の変更箇所は省略してレビューされました。"

    post_review(github_repository, pr_number, github_token, head_sha, body, comments)

if __name__ == "__main__":
    main()
