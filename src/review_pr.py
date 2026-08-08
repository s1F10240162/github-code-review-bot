import os
import re
import sys
import json
import subprocess
import requests
from openai import OpenAI

MAX_DIFF_LENGTH = 20000  # トークン超過を防ぐための文字数上限
MAX_TREE_ENTRIES = 300   # リポジトリ構成として送るファイル数の上限
MAX_TREE_LENGTH = 4000   # リポジトリ構成として送る文字数の上限
IGNORE_TREE_PREFIXES = (".git/", "node_modules/", "dist/", "build/", "__pycache__/", ".venv/", "venv/")

SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}
SEVERITY_LABEL = {"high": "優先度: 高", "medium": "優先度: 中", "low": "優先度: 低"}

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
    (1) インラインコメント可能な (ファイルパス, 新ファイル側の行番号) の集合 (file_line_map)
    (2) このPRで実際に追加された行だけの集合 (added_line_map、静的解析結果の絞り込みに使用)
    (3) 行番号付きでAIに提示するための整形済みテキスト
    を作成します。行番号は新ファイル側 (追加行・変更なしの周辺行) のみ付与され、
    削除された行にはコメントできないため番号を付けません。
    """
    is_truncated = len(diff_content) > MAX_DIFF_LENGTH
    if is_truncated:
        diff_content = diff_content[:MAX_DIFF_LENGTH]

    file_line_map = {}
    added_line_map = {}
    annotated_blocks = []

    current_file = None
    current_lines = {}
    current_added_lines = set()
    annotated_lines = []
    new_line_no = None

    def flush_file():
        if current_file and annotated_lines:
            file_line_map[current_file] = current_lines
            added_line_map[current_file] = current_added_lines
            annotated_blocks.append(f"■ ファイル: {current_file}\n" + "\n".join(annotated_lines))

    for raw_line in diff_content.splitlines():
        if raw_line.startswith("diff --git "):
            flush_file()
            current_file, current_lines, current_added_lines, annotated_lines, new_line_no = None, {}, set(), [], None
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
                current_added_lines.add(new_line_no)
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
    return file_line_map, added_line_map, "\n\n".join(annotated_blocks), is_truncated

def run_static_analysis(added_line_map):
    """
    変更されたPythonファイルに対して ruff (lint) と mypy (型チェック) を実行し、
    このPRで実際に追加された行に絞って、AIの findings と同じ形式の指摘リストを返します。
    ツール自体が存在しない/失敗した場合は警告を出すだけでスキップします（レビュー全体は継続）。
    """
    py_files = [f for f in added_line_map if f.endswith(".py") and os.path.isfile(f)]
    if not py_files:
        return []

    findings = []

    # --- ruff (lint) ---
    try:
        result = subprocess.run(
            ["ruff", "check", "--select=E,W,F,B,S,SIM,C4", "--output-format=json", *py_files],
            capture_output=True, text=True, timeout=60
        )
        if result.stdout.strip():
            for item in json.loads(result.stdout):
                file_path = os.path.relpath(item.get("filename", "")).replace(os.sep, "/")
                line = item.get("location", {}).get("row")
                if line in added_line_map.get(file_path, set()):
                    code = item.get("code", "?")
                    if re.match(r"^S\d", code):
                        severity = "high"  # flake8-bandit: セキュリティ上の懸念
                    elif code.startswith("B"):
                        severity = "medium"  # flake8-bugbear: バグになりやすいパターン
                    else:
                        severity = "low"
                    findings.append({
                        "file": file_path,
                        "line": line,
                        "severity": severity,
                        "title": f"[ruff:{code}] {item.get('message', '')}",
                        "body": f"静的解析ツール `ruff` による自動検出です（ルール: `{code}`）。"
                    })
    except FileNotFoundError:
        print("Warning: ruffが見つかりません。requirements.txtへの追加を確認してください。")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"Warning: ruffの実行結果を処理できませんでした: {e}")

    # --- mypy (型チェック) ---
    try:
        result = subprocess.run(
            ["mypy", "--ignore-missing-imports", "--follow-imports=silent",
             "--no-error-summary", "--show-column-numbers", *py_files],
            capture_output=True, text=True, timeout=120
        )
        pattern = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)?\s*(?P<level>error|warning|note):\s*(?P<message>.+)$")
        for line_out in result.stdout.splitlines():
            m = pattern.match(line_out)
            if not m:
                continue
            file_path = os.path.relpath(m.group("file")).replace(os.sep, "/")
            line = int(m.group("line"))
            if line in added_line_map.get(file_path, set()):
                level = m.group("level")
                findings.append({
                    "file": file_path,
                    "line": line,
                    "severity": "medium" if level == "error" else "low",
                    "title": f"[mypy:{level}] 型チェック",
                    "body": f"静的解析ツール `mypy` による自動検出です。\n\n{m.group('message')}"
                })
    except FileNotFoundError:
        print("Warning: mypyが見つかりません。requirements.txtへの追加を確認してください。")
    except subprocess.TimeoutExpired as e:
        print(f"Warning: mypyの実行がタイムアウトしました: {e}")

    if findings:
        print(f"静的解析で {len(findings)} 件の指摘を検出しました（ruff/mypy）。")
    return findings

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
    file_line_map, added_line_map, annotated_diff, is_truncated = analyze_diff(diff_content)

    review_data, raw_fallback_text = generate_review(
        title, pr_body, annotated_diff, repo_tree, model_name, openai_api_key, openai_base_url
    )

    if review_data is None:
        post_issue_comment(github_repository, pr_number, github_token, raw_fallback_text or "レビュー結果の取得に失敗しました。")
        return

    if os.getenv("ENABLE_STATIC_ANALYSIS", "true").lower() != "false":
        static_findings = run_static_analysis(added_line_map)
        review_data["findings"] = (review_data.get("findings") or []) + static_findings

    body, comments = build_review_comments(review_data, file_line_map)
    if is_truncated:
        body += "\n\n> ⚠️ **注意**: 差分量が多いため、一部の変更箇所は省略してレビューされました。"

    post_review(github_repository, pr_number, github_token, head_sha, body, comments)

if __name__ == "__main__":
    main()
