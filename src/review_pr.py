import os
import sys
import json
import requests
from openai import OpenAI

MAX_DIFF_LENGTH = 20000  # トークン超過を防ぐための文字数上限
MAX_TREE_ENTRIES = 300   # リポジトリ構成として送るファイル数の上限
MAX_TREE_LENGTH = 4000   # リポジトリ構成として送る文字数の上限
IGNORE_TREE_PREFIXES = (".git/", "node_modules/", "dist/", "build/", "__pycache__/", ".venv/", "venv/")

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

def generate_review(title, pr_body, diff_content, repo_tree, model_name, api_key, base_url=None):
    """OpenAI API（または大学等のプロキシAPI）を呼び出してコードレビュー結果を生成します。"""
    if base_url:
        print(f"カスタムBase URLを使用中: {base_url}")
        client = OpenAI(api_key=api_key, base_url=base_url)
    else:
        client = OpenAI(api_key=api_key)

    is_truncated = False
    if len(diff_content) > MAX_DIFF_LENGTH:
        diff_content = diff_content[:MAX_DIFF_LENGTH] + "\n\n... (差分が長すぎるため一部省略されました)"
        is_truncated = True

    system_prompt = """あなたは経験豊富なシニアソフトウェアエンジニア兼コードレビュアーです。
提出されたPull Requestの変更差分(Diff)を、リポジトリ全体のディレクトリ構成やPRの説明（意図）と照らし合わせながら分析し、以下のガイドラインに従って日本語でコードレビューを行ってください。

【コンテキストの扱い方】
- 「リポジトリ構成」が提供されている場合は、変更が既存の設計・ファイル配置・命名規則に沿っているか（置き場所の妥当性、既存モジュールとの重複がないか等）を確認材料にしてください。
- PRの説明が「説明なし」またはごく簡素で意図が読み取れない場合、憶測で断定した評価をせず、改善・懸念事項の中で「⚠️ 意図の確認: 背景・目的をPR概要に追記してください」と一言添えてください。特にコードの一部分だけを機械的に差し替えたように見える変更は、周辺ファイルや呼び出し元との整合性を慎重に確認してください。

【レビュー項目・フォーマット】
1. 📝 **変更概要のサマリー**: 何が変更されたかを簡潔に要約してください。
2. 🌟 **良かった点**: コードの品質、設計、テスト、命名規則など優れている点を褒めてください。
3. ⚠️ **改善・懸念事項**:
   - 🔴 **【優先度: 高】** バグの可能性、セキュリティ上の脆弱性、重大なパフォーマンストラブル
   - 🟡 **【優先度: 中】** 可読性・保守性の低下、例外処理の不足、エッジケースの考慮漏れ、既存のディレクトリ構成・命名規則との不整合
   - 🔵 **【優先度: 低】** 軽微なリファクタリング提案、命名の改善、タイポなど
4. 💡 **具体的な修正提案**: 改善点がある場合は、Markdownのコードブロック形式で修正後のコード例を提示してください。

※ 建設的で丁寧なトーンで回答してください。大きな問題がなければ「問題なし」と評価し、リリースを後押ししてください。
"""

    repo_tree_section = f"""
■ リポジトリ構成 (参考・一部のみの場合あり):
```
{repo_tree}
```
""" if repo_tree else ""

    user_prompt = f"""以下はレビュー対象のPull Request情報です。

■ PRタイトル: {title}
■ PR概要:
{pr_body}
{repo_tree_section}
■ 変更差分 (Git Diff):
```diff
{diff_content}
```
"""

    print(f"モデル '{model_name}' を使用してレビューを生成中...")

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3
        )
        review_text = response.choices[0].message.content
        if is_truncated:
            review_text += "\n\n> ⚠️ **注意**: 差分量が多いため、一部の変更箇所は省略してレビューされました。"
        return review_text
    except Exception as e:
        print(f"Error: OpenAI API呼び出しに失敗しました: {e}")
        sys.exit(1)

def post_comment_to_pr(repo, pr_number, token, comment_body):
    """PRにコメントを投稿します。"""
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
    if response.status_code not in [200, 201]:
        print(f"Error: PRへのコメント投稿に失敗しました (Status: {response.status_code}): {response.text}")
        sys.exit(1)
    
    print(f"Success: PR #{pr_number} にコードレビューを正常に投稿しました！")

def main():
    openai_api_key = get_env_variable("OPENAI_API_KEY")
    openai_base_url = os.getenv("OPENAI_BASE_URL") or "https://api.openai.iniad.org/api/v1"
    github_token = get_env_variable("GITHUB_TOKEN")
    github_repository = get_env_variable("GITHUB_REPOSITORY")
    
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

    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

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

    review_result = generate_review(title, pr_body, diff_content, repo_tree, model_name, openai_api_key, openai_base_url)
    post_comment_to_pr(github_repository, pr_number, github_token, review_result)

if __name__ == "__main__":
    main()
