# 🤖 GitHub Actions AI Code Review BOT (OpenAI API)

GitHubのPull Request (PR) が作成・更新された際に、OpenAI API (GPT-4o / GPT-4o-mini) を使って自動でコードレビューを行い、コメントを投稿するBotの構築キットです。

---

## 📁 ディレクトリ構造

```text
github-code-review-bot/
├── .github/
│   └── workflows/
│       └── code-review.yml   # GitHub Actions のワークフロー定義ファイル
├── src/
│   └── review_pr.py          # Diff取得・OpenAIレビュー生成・コメント投稿を行うメインPythonスクリプト
├── .gitignore
├── requirements.txt          # 依存パッケージ (openai, requests)
└── README.md                 # 導入・設定マニュアル (本ファイル)
```

---

## 🛠️ 導入手順（3ステップ）

### ステップ 1: ファイルの配置
作成したいGitHubリポジトリのルートに、本フォルダ内の以下のファイル・フォルダをコピーしてコミット＆プッシュします。
- `.github/workflows/code-review.yml`
- `src/review_pr.py`
- `requirements.txt`

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
[review_pr.py](file:///C:/Users/iniad/Documents/github-code-review-bot/src/review_pr.py) 内の `system_prompt` を編集することで、プロジェクト固有のコーディング規約（例: TypeScriptの型指定チェック、テストコード必須化など）に合わせたレビューを行わせることができます。

---

## 🔒 セキュリティ & コスト管理

- **APIキーの保護**: APIキーは GitHub Secrets で安全に保持され、ログに漏洩することはありません。
- **コスト保護**: `review_pr.py` 内で文字数上限 (`MAX_DIFF_LENGTH = 20000`) を設定しており、巨大な差分によってAPI利用料金が跳ね上がるのを防いでいます。
