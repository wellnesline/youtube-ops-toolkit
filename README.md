# youtube-ops-toolkit

Scripts for operating YouTube channels through the Data API v3: uploads through a human-approved queue (private by default), playlist sync/backfill, channel metadata sync and bulk description updates.

YouTube Data API v3 を使ったチャンネル運用の CLI 集です。予約公開つきのアップロード、アップロードキュー、プレイリスト同期、チャンネル情報の同期、概要欄の一括置換を扱います。公開用に整理したサンプル実装です。

## 概要

動画の量産運用では、API の日次クォータ、公開時刻の重複、うっかりの本番投稿が問題になります。このツール群は、設定ファイル（`channel.yaml`）を一次情報にして、次のような安全側の設計で運用を自動化します。

## 主な機能・設計のポイント

- **アップロードキュー（`upload_queue.py`）**: 投入（submit）と実行（worker）を分離。ワーカーが 1 件ずつ順にアップロードします。日次クォータの上限管理はせず、超過時は API の 403 で止まります（目安: 1 本あたり約 150 units）。`submit` は全動画を private・予約なしで投入し、公開予約は人が YouTube Studio で設定します（空き枠の自動割当は現行の `submit` では使わず、関数 `_next_publish_slot_yt_aware` は残置）。ワーカーは多重起動を防ぐロックを取ります
- **投稿の安全装置（`upload_youtube.py`）**: 予約公開（`--publish-at`）は任意です。省略すると private で投入し、指定した場合は private + publishAt で予約公開して、`PUBLISH_HOURS`（環境変数。公開スロットの時刻、JST。既定値なし）に合う時刻かを検査します。`upload_youtube.py` はキュー worker 経由（環境変数 `UPLOAD_VIA_QUEUE`＋worker のロックファイル＋親プロセスの確認）でしか起動できず、そのキューへの投入（`upload_queue.py submit`）も、人が付ける `HUMAN_TRIGGERED=1` か有効な承認マーカー（`.stage4_approved.yaml` の `approved_at`）が無いと通りません。ガードは関数本体にも入れて、import 経由の迂回を防ぎます。手動投入用の `upload_post_youtube.py` も、キューガードは既定で有効、公開範囲の既定は private です。ガードを外すには `--bypass-queue-guard`、公開するには `--visibility public` をそれぞれ明示します。`UPLOAD_DRY_RUN=1` では、YouTube API の呼び出し・OAuth の開始・ローカル記録（`log.yaml`・`_progress.yaml`・`upload_*.json`）の書き込みをいずれも行わず、投入内容の表示だけで終了します。サムネイルが上限（2MB）を超える場合は `upload_youtube.py` が Pillow で段階的に圧縮し、Pillow が無ければそのままアップロードします
- **フック**: アップロード成功直後に、`channel.yaml` の定義に従ってプレイリストへ自動追加します（`upload_playlist_hook.py`）
- **プレイリスト（`yt_playlists.py` / `yt_playlist_sync.py` / `yt_playlist_backfill.py`）**: 作成・更新・追加の薄いラッパ。重複追加は no-op にして、何度実行しても安全（冪等）にしています。クォータ超過（403）は正常終了して翌日に再開できます。`ruamel.yaml` のラウンドトリップで、ID を書き戻してもコメントや順序を壊しません
- **チャンネル情報の同期（`yt_channel_sync.py`）**: `channel.yaml` のキーワード・説明文・言語などを YouTube 側へ反映
- **概要欄の置換（`update_youtube_url_only.py`）**: 置換テーブルを JSON で渡し、旧 URL を新 URL に置き換えるだけの更新
- **認証（`yt_auth.py`）**: OAuth の初回フローで token を保存し、以降はリフレッシュトークンで自動認証します

## ディレクトリ構成（主要ファイル）

```
youtube-ops-toolkit/
├ upload_queue.py  upload_youtube.py  upload_post_youtube.py  upload_playlist_hook.py
├ yt_auth.py  yt_playlists.py  yt_playlist_sync.py  yt_playlist_backfill.py  yt_channel_sync.py
├ update_youtube_url_only.py
├ src/channel_resolver.py  src/credential_store.py     ← 合成スタブ
└ requirements.txt
```

## 動かし方

- 必要なもの: Linux / macOS（キューの排他制御に `fcntl`、worker の確認に `ps` を使うため POSIX 前提）、Python 3、`requirements.txt` の依存、YouTube Data API を有効にした Google Cloud の OAuth クライアント
- 環境変数: `CHANNEL` / `DEFAULT_CHANNEL`（対象チャンネル名）、`PUBLISH_HOURS`（`--publish-at` の時刻検査に使う公開スロットの時刻。既定値なし）、`SHORT_VARIANTS`（本編に付ける短尺の種類。既定は無し）、`SHORTS_OFFSET_MINUTES`（短尺の公開オフセット。既定 0）、`STAGE4_MARKER_TTL_HOURS`（承認マーカーの有効時間）、`MAIN_LINK_LABEL` / `AI_DISCLOSURE_TEXT` / `EXTRA_CREDIT_TEXT`（概要欄の固定文言。下記）、`POST_VIDEO_ROOT`（動画の置き場）、`YT_DEFAULT_CATEGORY_ID`（動画カテゴリの既定。未設定なら 22）、`UPLOAD_DRY_RUN`（ドライラン）、`URL_REPLACE_CONFIG`（置換設定 JSON）、`YT_AUTH_NONINTERACTIVE`
- 配置: スクリプトと同じ階層の `src/`（同梱の合成スタブ `channel_resolver.py` / `credential_store.py`）を import します。このリポジトリのルートで、そのまま `python yt_auth.py` のように実行できます（ホストプロジェクトの `src/` 直下にスクリプトを置く配置でも、親ディレクトリを基準に動きます）。`channels/<channel>/` はルート基準です
- 手順の例: `python yt_auth.py`（初回認証）→ `HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug>`（private・予約なしで投入。承認マーカーがあれば `HUMAN_TRIGGERED` は不要。`review/<slug>.mp4` に本編を置いておく必要があり、無いと submit は止まります）→ `python upload_queue.py worker`。キュー・ロック・動画置き場は既定チャンネル（`CHANNEL` / `DEFAULT_CHANNEL`）の下に作られるため、別チャンネルは `CHANNEL=<名前>` を環境変数で指定して実行します。承認マーカーの生成スクリプトは同梱していないので、`approved_at: <ISO8601>` を書いたファイルを人が置きます。予約公開が必要なら、(1) キューのジョブ JSON の `publish_at` に ISO8601 の日時を入れる、または (2) `upload_post_youtube.py --publish-at <ISO8601 UTC> --bypass-queue-guard` で手動投入します（`upload_youtube.py` はキュー worker 経由でしか起動できません）
- 概要欄の固定文言（本編リンクの見出し、AI 生成物の開示文、合成音声ライブラリなどのクレジット表記）は環境変数で渡します。同梱の既定値は動作確認用の中立な文言です。合成音声などを使う場合は、使う音声・素材の利用規約が求める表記を `EXTRA_CREDIT_TEXT` に設定してください
- 設定ファイル `channels/<channel>/channel.yaml`（プレイリスト定義、アップロード既定値、チャンネルメタ）は同梱していません。スクリプト冒頭の docstring にキーの例があります

## 制限事項（閉包）

`ast` と import 文の調査で、リポジトリ内・標準ライブラリに無く、`requirements.txt` にも無い import は次の 1 件です（`google.*` は `requirements.txt` の Google 系パッケージで解決されます）。

- **同梱していない Python パッケージ（1 件）**: `PIL`（Pillow。サムネイル圧縮でのみ使用する任意の依存）
- **他の公開リポジトリへの依存**: なし
- **合成スタブ**: `src/channel_resolver.py`（`channels/<name>/channel.yaml` を引く最小実装）, `src/credential_store.py`（`yt_auth.py` が token の保存に使う関数 `save_credentials_atomic` のスタブ）。いずれも本来の実装ではありません
- **非同梱の実行時データ**: `channel.yaml`、OAuth クライアント情報とトークン、動画ファイルとメタデータ
- 外部サービス: YouTube Data API v3。利用は YouTube API サービス利用規約・各チャンネルの権限の範囲で行い、認証情報は環境やファイルで管理して、リポジトリに含めないでください（同梱の `.gitignore` は `channels/*/secrets/`、`token.json`、`.env` などを除外します。秘密情報はリポジトリの外に置くことを勧めます）

## ライセンス

All rights reserved. 閲覧目的で公開しています。詳細は [LICENSE](LICENSE) を参照。
