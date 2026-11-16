# lens-review-bot

GitHub Actions 上で [lens-review](https://github.com/reeschan/claude-plugins-official) を動かし、PR を自動でレビューする reusable workflow です。

- PR を開いたとき・draft を外したとき・push したとき・`/lens-review` とコメントしたときに起動し、`reeschan/claude-plugins-official` の既定ブランチにある最新の `lens-review` でレビューします。
- PR への投稿は重複を避け、まとめのコメントを 1 つに保って書き換えます。

> [!IMPORTANT]
> `reeschan/claude-plugins-official` の既定ブランチに **lens-review 0.3.0 以上** が入ってから利用してください（0.2 系では 2 回目以降にコメントが投稿されません）。

---

## 入れ方

### 1. Secret を登録する

対象リポジトリの Settings → Secrets and variables → Actions に、`ANTHROPIC_API_KEY` を登録します。

### 2. 呼ぶ側の workflow を置く

対象リポジトリの `.github/workflows/lens-review.yml` に、reusable workflow を呼び出す workflow を置きます（例: [`examples/caller.yml`](examples/caller.yml)）。

```yaml
name: lens-review

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  lens-review:
    uses: reeschan/lens-review-bot/.github/workflows/lens-review.yml@main
    # workflow を @<tag> で固定する場合は bot_ref にも同じ値を渡す
    # with:
    #   bot_ref: v0.1.0
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

- 呼ぶ側の `permissions`（`contents: read`・`pull-requests: write`・`issues: write`）は省けません。
- 安全のため workflow の参照は tag またはコミット SHA に固定し、スクリプトや既定設定の版も揃えるため、入力 `bot_ref` にも同じ値を渡してください（既定値は `main`）。

### 3. 設定ファイルを置く（任意）

設定を変える場合は、対象リポジトリの `.github/lens-review.yml` を置きます（例: [`examples/lens-review.yml`](examples/lens-review.yml)）。置かなければ既定値で動きます。

---

## 設定の項目

| 項目 | 既定 | 意味 |
|---|---|---|
| `model` | `claude-sonnet-5-5` | レビューに使うモデル |
| `max_turns` | `80` | ターンの上限 |
| `min_severity` | `major` | PR に出す重大度の下限 (`critical`・`major`・`minor`) |
| `post_rule_violations` | `true` | 規約に反する指摘を、重大度に関係なく出すか |
| `rules` | なし | 追加の規約のパスの一覧 |
| `spec` | なし | 仕様のパス |
| `exclude` | なし | 見ないパスの一覧 (glob) |
| `allowed_tools` | なし | 許す操作に足すもの |

### 設定ファイルの書式

設定ファイルは標準ライブラリだけで読むため、**YAML の部分集合**のみを受け付けます。解釈できない書式、知らない項目、型の違い、値の範囲外や禁じられた文字がある場合はレビューを回さず、PR にエラーの理由をコメントして失敗します。

- 最上位の `key: value` のみ（入れ子の対応表は不可、同じキーの重複は不可、タブによる字下げは不可）。
- 空行、`#` で始まる行、引用符の外の行末 ` #` 以降は無視します。
- 値として使えるもの:
  - 真偽値: `true` / `false`
  - 整数: `max_turns` は `1` 以上 `500` 以下（真偽値は不可）
  - 文字列: 引用符なし、または `'...'` / `"..."`
  - 一覧: フロー形式 `[a, b]` またはブロック形式（次の行から同じ字下げで `- item`）
- 主な制約:
  - `model`: `[A-Za-z0-9._:@/\[\]-]+` のみ（空白不可）
  - `rules`・`spec`: リポジトリからの相対パス（空不可、`/` 始まり不可、`..` 不可、空白・引用符・`$`・`` ` ``・`\`・改行不可）
  - `exclude`: 空不可、引用符・`$`・`` ` ``・`\`・改行不可
  - `allowed_tools`: 空不可、`,`・引用符・`$`・`` ` ``・`\`・改行不可

---

## 起動と飛ばす条件

### 起動するイベント

- `pull_request`: `opened`・`reopened`・`ready_for_review`・`synchronize`
- `issue_comment`: `created`（PR へのコメントで、先頭の空白を除いた本文が `/lens-review` で始まり、その直後が文字列の終わりまたは空白であるもの）

### 飛ばす条件

次の場合はレビューを実行せずにスキップします。

- `pull_request` イベントで PR が **draft** のとき（`/lens-review` コメントでは draft でも実行します）
- **fork** からの PR
- **bot が開いた PR**（`dependabot` など、`user.type == "Bot"` の場合。secret が渡らず失敗するため）
- 閉じた（`closed`）PR へのコメント、PR 以外の issue へのコメント、`/lens-review` で始まらないコメント（`/lens-reviewer` などは対象外）
- `issue_comment` でコメントした人にリポジトリへの書き込み権限（`admin`・`maintain`・`write`）が無いとき

同じ PR に対するレビューは `concurrency` により 1 つにまとめられ、新しい実行が始まると古い実行はキャンセルされます（`cancel-in-progress: true`）。job 全体の上限は 30 分、Claude 実行 step の上限は 25 分です。

---

## PR に出るもの

- **インラインコメント**: `critical`・`major` の指摘と、`post_rule_violations: true` のときはリポジトリの規約（`CLAUDE.md`・`AGENTS.md`・`rules`）に反する指摘を重大度に関係なく出します（下限は `min_severity` で変更できます）。
  - 既存のインラインコメントに付いた印（`<!-- lens-review: ... -->`）と同じ指摘は重ねて出しません。
  - レビュー種別は `COMMENT` のみで、承認や変更要求はしません。
- **まとめのコメント**: 1 行目が `## lens-review` の `github-actions[bot]` によるコメントを 1 つだけ作成・書き換えます。出さなかった `minor` の件数などもまとめに載ります。

---

## 失敗したとき

- `.github/lens-review.yml` が読めない・不正な場合は、理由と base SHA をまとめのコメントに書き、job は失敗します。
- PR 情報の取得・チェックアウト・Claude の実行（25 分の時間切れを含む）に失敗した場合は、失敗した段・Actions ログへのリンク・取得できた対象の head SHA をまとめに書き、job は失敗します。
- 新しい push などにより実行がキャンセルされた場合は、失敗のコメントは書きません（後続の実行がまとめを更新します）。

---

## 安全について

- **fork からの PR は回りません**: `pull_request_target` を使わず、fork からの PR はスキップするため、外部コードと同じ環境に `ANTHROPIC_API_KEY` が置かれません。
- **設定は base ブランチの版を読みます**: `.github/lens-review.yml` は PR の base ブランチから API で取得するため、PR 内で設定を変えて許可ツールを増やしたり条件を緩めたりしても、その PR のレビューには効きません。
- **`.github/lens-review.yml` 以外は PR の head の版が動きます**: 呼ぶ側の workflow 自体（`.github/workflows/lens-review.yml`）や、チェックアウトして読む `CLAUDE.md`・`AGENTS.md`・`rules`・`spec` は PR の head の版が使われます。
- **コードは既定で実行しません**: テスト・インタプリタ・パッケージマネージャなどは既定の許可ツールに含めておらず、`--permission-mode default` と `--setting-sources user` で実行を制限しています。必要なコマンドのみ `allowed_tools` で明示的に追加できます。
- **Git の書き出しを制限します**: `--output` で任意のファイルに書ける `git diff`・`git show`・`git log` と、任意のコマンドを実行できる `git fetch` は既定では許可しません。
- **public リポジトリではログが公開されます**: GitHub Actions の実行ログには `lens-review` の端末向け出力（PR に出さなかった `minor` や要確認項目を含む）が出力される点に注意してください。

---

## 開発

スクリプト（標準ライブラリのみ）の単体テストを実行します。

```sh
python -m unittest discover -s tests
```
