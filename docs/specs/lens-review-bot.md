# lens-review bot: PR の自動レビュー

## Problem Statement

lens-review は、裏を取った指摘だけを日本語で出すレビューのスキルだが、今は手元で呼んだときにしか動かない。PR を開くたびに自分で回す手間がかかり、回し忘れた PR は見られないままになる。CodeRabbit のように PR に自動でレビューを付けたいが、CodeRabbit は観点もモデルも選べず、リポジトリごとの規約を確実には反映しない。また、lens-review を直しても、自動のレビューに新しい版がすぐには届かない。

## Solution

GitHub Actions で lens-review を動かす bot を作る。PR を開いたとき、draft を外したとき、push のたび、`/lens-review` とコメントしたときに起動し、`reeschan/claude-plugins-official` の既定ブランチの最新の lens-review で PR をレビューする。PR に出すのは critical・major の指摘と、そのリポジトリの規約に反する指摘だけ。コメントの重なりを避け、まとめのコメントは 1 つを書き換える。モデル・ターン数・出す重大度の下限・追加の規約・見ないパス・許す操作は、リポジトリごとの設定ファイルで変えられる。

bot は public のリポジトリ `reeschan/lens-review-bot` に reusable workflow として置き、使うリポジトリには数行の workflow と設定ファイルだけを置く。自分のリポジトリでも、試しに入れる forgers のリポジトリでも、同じ形で使う。

## User Stories

1. As a リポジトリの持ち主, I want PR を開くと自動でレビューが付く, so that 回し忘れが無くなる
2. As a リポジトリの持ち主, I want draft のあいだは自動で回らない, so that 書きかけの PR で費用がかからない
3. As a リポジトリの持ち主, I want draft を外した時点でレビューが付く, so that 見てほしくなったときに見てもらえる
4. As a リポジトリの持ち主, I want push のたびにレビューが回り直す, so that 直した後の状態も見られる
5. As a リポジトリの持ち主, I want 回り直したときに、前と同じ指摘が重ねて付かない, so that 通知とコメントが増えすぎない
6. As a リポジトリの持ち主, I want まとめのコメントが 1 つのまま書き換わる, so that PR の会話が流れない
7. As a リポジトリの持ち主, I want 続けて push したら、古いレビューは止まって新しい方だけが回る, so that 古い差分への指摘と費用が出ない
8. As a 書き込み権限のある人, I want `/lens-review` とコメントすればもう一度回せる, so that 必要なときに見直せる
9. As a リポジトリの持ち主, I want 書き込み権限の無い人のコメントでは回らない, so that 他人に費用を使われない
10. As a リポジトリの持ち主, I want PR に出るのは critical と major だけ, so that 重要な指摘が埋もれない
11. As a リポジトリの持ち主, I want そのリポジトリの規約 (`CLAUDE.md`・`AGENTS.md`・設定に書いた規約のファイル) に反する指摘は、重大度に関係なく出る, so that 規約がレビューに確実に反映される
12. As a リポジトリの持ち主, I want 出さなかった minor の件数がまとめに載る, so that 何件あったかは分かる
13. As a リポジトリの持ち主, I want 出す重大度の下限をリポジトリごとに変えられる, so that 小さなリポジトリでは minor まで見られる
14. As a リポジトリの持ち主, I want モデルとターン数をリポジトリごとに変えられる, so that 重いリポジトリには重いモデルを、軽いものには安いモデルを使える
15. As a リポジトリの持ち主, I want 追加の規約や仕様のファイルを設定で渡せる, so that `CLAUDE.md` に書いていない決まりも反映される
16. As a リポジトリの持ち主, I want 見ないパスを設定できる, so that 生成物やロックファイルに費用を使わない
17. As a リポジトリの持ち主, I want 設定を書かなければ既定値で動く, so that 入れるのに手間がかからない
18. As a リポジトリの持ち主, I want lens-review を直してマージすると、次のレビューから新しい版が使われる, so that bot を別に更新しなくてよい
19. As a リポジトリの持ち主, I want lens-review の入れ込みやレビューが失敗したとき、まとめに失敗と書かれる, so that 黙って見られないままにならない
20. As a リポジトリの持ち主, I want PR の中で設定ファイルを書き換えても、その PR のレビューには効かない, so that 許す操作を広げる・レビューを外すといった抜け道が無い
21. As a リポジトリの持ち主, I want PR の中で規約を直したときは、直した規約でレビューされる, so that 規約の変更と実装の変更を 1 つの PR で出せる
22. As a リポジトリの持ち主, I want fork からの PR では回らない, so that secret が外のコードと同じ場所に置かれない
23. As a リポジトリの持ち主, I want レビューがリポジトリのコードを既定では実行しない, so that PR のコードが secret のある環境で動かない
24. As a リポジトリの持ち主, I want 実行してよいコマンドを設定で足せる, so that 依存の要らない検査なら裏取りに使える
25. As a 会社のリポジトリの担当者, I want 会社の API キーで動かせる, so that 費用とコードを渡す先の責任が会社のものとして分かれる
26. As a 会社のリポジトリの担当者, I want 組織への GitHub App のインストールなしで試せる, so that 管理者の承認を待たずに試せる
27. As a リポジトリの持ち主, I want 呼ぶ側の workflow が数行で済む, so that リポジトリを増やすのが楽になる
28. As a リポジトリの持ち主, I want CodeRabbit が入っているリポジトリでは指摘を見比べられる, so that lens-review の拾い方を評価できる
29. As a lens-review の開発者, I want 手元で `--comment` を回したときも、bot と同じ投稿の決まりで動く, so that 手元で bot の投稿を確かめられる
30. As a リポジトリの持ち主, I want レビューが 30 分を超えたら止まる, so that 暴走して費用がかかり続けない

## Implementation Decisions

**構成**

- 新しい public のリポジトリ `reeschan/lens-review-bot` を作る。中身は reusable workflow、設定を読むスクリプト、既定の設定、README、仕様。秘密は入れない。private の reusable workflow は別のアカウントや組織からは呼べないので、public にする。
- 使うリポジトリには、reusable workflow を呼ぶ workflow (数行) と、任意の設定ファイル `.github/lens-review.yml` を置く。secret `ANTHROPIC_API_KEY` は使うリポジトリごとに入れる (個人アカウントには組織の secret が無い)。reeschan のリポジトリは持ち主のキー、forgers のリポジトリは会社が出すキー。
- 実行は `anthropics/claude-code-action@v1`。認証は `anthropic_api_key`。lens-review は `plugin_marketplaces` に `https://github.com/reeschan/claude-plugins-official.git`、`plugins` に `lens-review@reeschan-plugins` を渡して、毎回入れる。版は固定しない (既定ブランチ `reeschan` の最新)。
- コメントは workflow の `GITHUB_TOKEN` で付ける (`github-actions[bot]` の名前)。claude-code-action の `github_token` に渡す。`lens-review[bot]` の名前にしたくなったら、自分の GitHub App に移す (今回はしない)。

**起動と飛ばす条件**

- 起動: `pull_request` の `opened`・`reopened`・`ready_for_review`・`synchronize` と、`issue_comment` の `created` (本文が `/lens-review` で始まる PR のコメント)。
- 飛ばす: draft の PR への `pull_request` のイベント。fork からの PR (head のリポジトリが base と違う)。コメントした人がリポジトリに書き込み権限を持たない。`/lens-review` で始まらないコメント。`pull_request_target` は使わない。
- 同じ PR のレビューは `concurrency` で 1 つにまとめ、`cancel-in-progress` で古い方を止める。job の時間の上限は 30 分。

**設定**

- 設定ファイル `.github/lens-review.yml` の項目:

  | 項目 | 既定 | 意味 |
  |---|---|---|
  | `model` | `claude-sonnet-5-5` | レビューに使うモデル |
  | `max_turns` | 80 | ターンの上限 |
  | `min_severity` | `major` | PR に出す重大度の下限 (`critical`・`major`・`minor`) |
  | `post_rule_violations` | `true` | 規約に反する指摘を、重大度に関係なく出すか |
  | `rules` | なし | 追加の規約のパスの一覧 |
  | `spec` | なし | 仕様のパス |
  | `exclude` | なし | 見ないパスの一覧 (glob) |
  | `allowed_tools` | なし | 許す操作に足すもの |

- 設定は PR の base ブランチの版から読む (`git show <base の SHA>:.github/lens-review.yml`)。PR の中で設定を書き換えても、その PR には効かない。無ければ既定値。読めない (YAML として壊れている、知らない項目がある、型が違う) ときはレビューを回さず、まとめのコメントに理由を書く (既定値で黙って回さない)。
- 規約 (`CLAUDE.md`・`AGENTS.md`・`rules` に書いたファイル) と仕様は、PR の head の版を使う。lens-review は作業ツリーから読むので、head をチェックアウトする。
- 規約は、リポジトリにあるファイルだけから取る。手元の shared-memory は使わない。

**設定を読むスクリプト (テストの境界 1)**

- 入力: PR のイベント (種類・draft か・head と base のリポジトリ・コメントの本文・コメントした人の権限) と、base の版の設定ファイルの中身 (無ければ無し)。
- 出力: 「回す」か「飛ばす (理由)」か「設定の誤り (理由)」のどれか。回すときは、claude-code-action に渡す `claude_args` と `prompt`。
- `claude_args` には `--model`・`--max-turns`・`--permission-mode default`・`--allowedTools` を入れる。許す操作の既定は、読む道具 (Read・Grep・Glob)、サブエージェント (Agent)、`git`、`gh pr view`・`gh pr diff`・`gh pr comment`・`gh api`、インラインコメントの道具、一時フォルダへの書き込み。リポジトリのコードの実行 (テスト・インタプリタ・パッケージマネージャ) は既定に入れず、設定の `allowed_tools` でだけ足せる。claude-code-action の既定の許可の仕方 (auto) では広い許可が無視されるので、`--permission-mode default` にして列挙を厳密に効かせる。
- `prompt` は lens-review を呼ぶ指示: `/lens-review:lens-review <PR 番号> --comment --min-severity <値> [--post-rule-violations] [--rules <パス>]... [--spec <パス>]`。`exclude` は、対象のパスから除く指示として渡す。
- スクリプトは外部に依存しない (標準ライブラリだけ) 小さなものにし、reusable workflow から呼ぶ。

**lens-review の変更 (`reeschan/claude-plugins-official`、テストの境界 2)**

- `--min-severity <critical|major|minor>` を足す。PR に投稿するときだけ効き、端末への出力は変えない。既定は `minor` (今と同じく全部出す)。
- `--post-rule-violations` を足す。「規約と仕様」の観点の指摘のうち、リポジトリの `CLAUDE.md`・`AGENTS.md` と `--rules` のファイルに反するものは、`--min-severity` より軽くても投稿する。仕様や ADR との食い違いは、ほかの指摘と同じく `--min-severity` に従う。
- `--comment` を「書き換えるモード」にする。今の「まとめのコメントがもうあれば投稿しない」をやめる。
  - 既存のインラインコメントの印 (`<!-- lens-review: <file>:<行> <見出し> -->`) と同じ指摘は出さない。
  - 新しい指摘は 1 つのレビューにまとめて付ける。
  - まとめのコメント (1 行目が「## lens-review」) があれば書き換え、無ければ足す。
- まとめのコメントには、重大度ごとの件数、出さなかった件数 (下限より軽いもの)、確かめた観点、レビューした head の SHA を書く。要確認は PR に出さない (今と同じ)。
- 版を上げる (0.3.0)。

**失敗の扱い**

- lens-review の入れ込み、Claude の実行、投稿のどれかが失敗したら、workflow の後片付けの段で、まとめのコメントを「レビューできなかった」と理由 (失敗した段とログへのリンク) に書き換える。job も失敗にする。
- 新しい push で止められたレビュー (cancel) は、失敗として書かない。次のレビューがまとめを書き換える。

**状態の図 (1 つの PR のレビュー)**

```mermaid
stateDiagram-v2
    [*] --> 待機
    待機 --> 判定: PR を開いた・draft を外した・push した・/lens-review とコメントした
    判定 --> 待機: 飛ばす (draft・fork・権限の無い人・関係の無いコメント)
    判定 --> 設定の誤りを報告: 設定ファイルが読めない
    設定の誤りを報告 --> 待機
    判定 --> レビュー中: 回す
    レビュー中 --> 判定: 新しい push かコメント (古いレビューは止める)
    レビュー中 --> 投稿済み: 新しい指摘を付け、まとめを書き換えた
    レビュー中 --> 失敗を報告: 入れ込み・実行・投稿のどれかが失敗した
    レビュー中 --> 失敗を報告: 30 分を超えた
    投稿済み --> 待機
    失敗を報告 --> 待機
    待機 --> [*]: PR を閉じた
    %% never 同じ PR に、まとめのコメントが 2 つある
    %% never 同じ指摘のインラインコメントが 2 つある
    %% never 同じ PR で、レビューが 2 つ同時に投稿する
    %% never fork からの PR で、secret を使ってレビューが回る
    %% never PR の中で書き換えた設定が、その PR のレビューに効く
```

## Testing Decisions

- よいテストは、外から見える振る舞いだけを確かめる。「このイベントと設定なら、回すか飛ばすか、何を渡すか」と「PR にどのコメントが付くか」を見て、中の関数の分け方には頼らない。
- 合意済みの seam:
  1. **設定を読むスクリプト**: イベントと設定ファイルの中身を入力に、「回す (渡す値)・飛ばす (理由)・設定の誤り (理由)」を出力として確かめる単体テスト。draft・fork・権限の無い人・関係の無いコメント・設定が無い・壊れた設定・知らない項目・型の違い・`allowed_tools` の足し方・既定値で、いちばん多くをここで確かめる。
  2. **lens-review の投稿**: テスト用の PR に対して手元で `--comment` を回し、`--min-severity` より軽い指摘が出ないこと、`--post-rule-violations` で規約違反が出ること、2 回目で同じ指摘が重ならず、まとめが書き換わることを確かめる。lens-review はスキルの本文なので、単体テストではなくこの形にする。
  3. **通しの確認**: テスト用の reeschan のリポジトリで 1 回、PR を開く → push する → `/lens-review` とコメントする、を順に動かす。インラインコメントが重ならないこと、まとめが 1 つのまま書き換わること、失敗を起こしたとき (例えば API キーを外す) にまとめが「レビューできなかった」になることを見る。
- 先例: lens-review の評価 (`plugins/lens-review/evals/`) は、過去の PR に対して回して CodeRabbit の指摘と突き合わせている。seam 2 はその回し方に倣う。

## Out of Scope

- 自分の GitHub App (`lens-review[bot]` の名前) への移行。
- lens-review の版の固定 (タグやコミットの指定)。
- fork からの PR のレビュー、`pull_request_target` の利用。
- PR のコードを実行する裏取り (依存の入れ込み、テストの実行) を既定で行うこと。
- shared-memory の review-guideline を bot で使うこと。
- 承認・変更要求のレビュー (bot は `COMMENT` だけを付ける)。
- 手元の端末でのレビューの振る舞いの変更 (要確認や minor の出し方は今のまま)。
- 組織の secret や、複数のリポジトリにまとめて入れる仕組み。

## Further Notes

- 入れる順: `reeschan/atelier` で試す → forgers のリポジトリ 1 つで試す (どれにするかは、そのときに決める) → ほかの reeschan のリポジトリに広げる。
- forgers のリポジトリに入れるのは、コードを外の CI と Anthropic の API に渡す変更になる。入れる前に、会社のキーと導入の許可を担当者に確かめる。
- CodeRabbit が入っているリポジトリでは、両方の指摘を見比べて lens-review の評価に使う。入っていないリポジトリでは見比べない。
- public のリポジトリでは Actions のログも公開される。lens-review の端末への出力 (要確認や minor を含む) がログに出る。
- claude-code-action の `plugin_marketplaces` でブランチを指定する書き方は、文書に無い。今は既定ブランチ `reeschan` が対象なので要らない。
- 状態の図の `%% never` のうち「同じ PR で、レビューが 2 つ同時に投稿する」と「新しいレビューが終わった後に、古いレビューが投稿する」は、古いレビューと新しいレビューを 2 つの領域にした図で TLC にかけ、どちらも成り立つことを確かめた (9 状態)。前提は、新しい push で古いレビューが止まること (`concurrency` の `cancel-in-progress`)。止まる前に投稿の途中だった分は、次のレビューが投稿済みの印で重ならないようにする。
- 用語: 「対象のリポジトリ」はレビューされるリポジトリ。「bot のリポジトリ」は `reeschan/lens-review-bot`。「まとめのコメント」は 1 行目が「## lens-review」の PR のコメント。「投稿済みの印」はインラインコメントの先頭の `<!-- lens-review: … -->`。
