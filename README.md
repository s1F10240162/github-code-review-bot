# 🤖 GitHub Actions AI Code Review BOT (OpenAI API)

GitHubのPull Request (PR) が作成・更新された際に、OpenAI API (GPT-4o / GPT-4o-mini) を使って自動でコードレビューを行い、コメントを投稿するBotの構築キットです。

---

## 📁 ディレクトリ構造

```text
github-code-review-bot/
├── .github/
│   ├── workflows/
│   │   └── code-review.yml   # GitHub Actions のワークフロー定義ファイル
│   └── PULL_REQUEST_TEMPLATE.md  # PR作成時の雛形（背景・目的の記載を促す）
├── src/
│   └── review_pr.py          # Diff取得・OpenAIレビュー生成・コメント投稿を行うメインPythonスクリプト
├── .gitignore
├── requirements.txt          # 依存パッケージ (openai, requests)
└── README.md                 # 導入・設定マニュアル (本ファイル)
```

---

## 🛠️ 導入手順（3ステップ）

### ステップ 1: ワークフローファイルの配置
導入したいGitHubリポジトリのルートに、以下の1ファイルのみをコピーしてコミット＆プッシュします。
- `.github/workflows/code-review.yml`

このワークフローは実行のたびに本リポジトリ (`s1F10240162/github-code-review-bot`) から `src/review_pr.py` と `requirements.txt` を自動取得して実行するため、`src/` や `requirements.txt` を導入先にコピーする必要はありません（コピーしても実行時には使用されません）。

任意ですが、`.github/PULL_REQUEST_TEMPLATE.md` も導入先リポジトリにコピーすることを推奨します。こちらはGitHub側の機能（PR作成フォームの雛形）のため、`src/review_pr.py`とは異なりリポジトリごとに個別配置が必要です。詳細は後述の「チーム開発での利用について」を参照してください。

### ステップ 2: OpenAI APIキーのセットアップ
1. [OpenAI Platform](https://platform.openai.com/api-keys) で API Key を発行します。
2. 対象の GitHub リポジトリを開きます。
3. **Settings** ➔ **Secrets and variables** ➔ **Actions** に移動します。
4. **New repository secret** をクリックし、以下を登録します。
   - **Name**: `OPENAI_API_KEY`
   - **Secret**: 発行したOpenAI APIキー (`sk-...`)

### ステップ 3: ワークフロー権限の確認
GitHub ActionsがPRにコメントを書き込めるように権限を設定します。
1. リポジトリの **Settings** ➔ **Actions** ➔ **General** を開きます。
2. **Workflow permissions** セクションで **Read and write permissions** を選択し、保存（Save）します。
   *(※ `.github/workflows/code-review.yml` 内でも `pull-requests: write` 権限を指定しています)*

---

## 🧪 動作確認 (テスト方法)

1. 新しいブランチを作成し、適当なコード変更（または追加）を行います。
2. そのブランチから Pull Request を作成します。
3. GitHub Actions が自動で起動し、数十秒〜1分程度で **AI Code Review コメント** がPRに自動追加されます！

---

## ⚙️ カスタマイズ

### モデルの変更 (`gpt-4o-mini` ⇄ `gpt-4o`)
`.github/workflows/code-review.yml` の `OPENAI_MODEL` 環境変数を編集することで切り替えられます。

```yaml
env:
  OPENAI_MODEL: 'gpt-4o' # 高精度・詳細な分析を行いたい場合は 'gpt-4o' に変更
```

- **`gpt-4o-mini`** (デフォルト): 高速かつ非常に安価。日常的なレビューに最適。
- **`gpt-4o`**: より複雑な設計思考や深いバグ検出を行いたい場合におすすめ。

### レビュー観点（プロンプト）の調整
ワークフローは常に本リポジトリの `src/review_pr.py` を取得して実行するため、導入先リポジトリで直接編集しても反映されません。プロジェクト固有のコーディング規約（例: TypeScriptの型指定チェック、テストコード必須化など）に合わせてレビュー内容をカスタマイズしたい場合は、本リポジトリをフォークし、[review_pr.py](file:///C:/Users/iniad/Documents/github-code-review-bot/src/review_pr.py) 内の `system_prompt` を編集した上で、導入先の `code-review.yml` 内の参照先 (`s1F10240162/github-code-review-bot` の箇所、2か所) を自分のフォーク先に書き換えてください。

### OpenAI APIのエンドポイント変更 (`OPENAI_BASE_URL`)
デフォルトでは INIAD AI MOP API (`https://api.openai.iniad.org/api/v1`) が使用されます。通常のOpenAI公式APIやその他のプロキシを使いたい場合は、リポジトリの Secrets に `OPENAI_BASE_URL` を追加してください。

---

## 👥 チーム開発での利用について

複数人でPRを出し合う環境では、AIに渡る情報の質がレビューの質を左右します。本Botは以下の工夫でこれに対応しています。

- **リポジトリ構成を自動で参照**: Diffだけでなく、リポジトリのファイル構成（`git/trees` API経由）もAIに渡しています。差分だけでは分からない「この変更は既存の設計・配置と整合しているか」まで踏み込んでレビューします。
- **意図が読み取れないPRへの配慮**: PR概要が空欄・簡素な場合、AIは憶測で評価を断定せず「⚠️ 意図の確認」を促すコメントを返します。特に初心者がコードの断片だけを貼り付けて修正させたようなPRでも、周辺ファイルとの整合性を優先的に確認します。
- **PRテンプレートの活用**: `.github/PULL_REQUEST_TEMPLATE.md` を用意しています。背景・目的・影響範囲を書く欄があり、ここに書いた内容がそのままAIレビューの文脈として使われます。チームメンバーには「空欄で出さない」よう周知してください。

---

## 🔒 セキュリティ & コスト管理

- **APIキーの保護**: APIキーは GitHub Secrets で安全に保持され、ログに漏洩することはありません。
- **コスト保護**: `review_pr.py` 内で差分の文字数上限 (`MAX_DIFF_LENGTH = 20000`) と、リポジトリ構成情報の上限 (`MAX_TREE_ENTRIES = 300` / `MAX_TREE_LENGTH = 4000`) を設定しており、巨大な差分や大規模リポジトリによってAPI利用料金が跳ね上がるのを防いでいます。
