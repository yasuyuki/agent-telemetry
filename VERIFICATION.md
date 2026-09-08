# 検証範囲

2026-09-08、Linux で検証。Python 3.14.4、Codex CLI 0.153.4、モデル
`gpt-6-astra`。Codex は環境の既存公開 `place.py list` / `start` 入口を使い、
認証 HOME と恒久設定を変更せず、README の OTel 設定を `-c` で試験実行だけに渡した。
Codex 本体の更新・再配置はこの検証に含めない。

## 自動試験

リポジトリ root で README のインストール手順後に実行する。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

22 テスト合格。合成 OTLP の属性変換・欠損・ゼロ・未知イベント・不正入力・
nanosecond 時刻保持、SQLite ファイルへの本文非混入、再読取と時刻検索、
並行 HTTP 保存、transaction 途中の SQL 失敗で全件 rollback、保存失敗の 503、
HTTP framing 拒否、loopback 制限、既存の異種 DB を変更しないことを確認した。

`test_plugin.py` は一時 venv にコアと独立 fixture パッケージをインストールし、
repo 外の cwd、Python `-I` で実行する。インストール先からの import と、
entry point で選んだ別プラットフォームの HTTP 200・SQLite 保存を確認する。
テストには venv / pip と build 依存を取得できる環境が必要。

独立レビューで検出した `success=NULL` の `false` 化を修正し、round-trip の
回帰テストを追加した。未選択プラグインを import しないことも検証した。

## Codex 実通信

無害な `printf telemetry-smoke-ok` を一度実行して終了する ephemeral セッションを使用。
Codex の JSON 出力でツール終了コード 0 と最終応答を確認した。
生 OTLP payload やツール本文を collector 側で記録せず、許可項目だけを
リポジトリ外の SQLite へ保存した。以下は合成作業の集計値であり、実セッション ID は掲載しない。

時刻対応後の検証セッションでは 11 行を保存した。

| イベント | 行数 | 確認した情報 |
| --- | ---: | --- |
| conversation_starts | 1 | CLI の thread ID と同じ session ID、モデル、CLI 版 |
| user_prompt | 1 | メタデータだけを保存、本文なし |
| websocket_request | 3 | 成否と所要時間 |
| sse_event | 3 | 種別別トークン数 |
| tool_decision | 1 | ツール名 |
| tool_result | 2 | exec_command と exec、両方 success=true、45 ms と 79 ms |

全行に `event.timestamp` からの発生時刻があり、受信時刻以前だった。
今回の exporter は標準 `timeUnixNano` を提供しなかったため、Codex 属性への対応を追加した。
受信終了後、修正版のインストール済みパッケージから別プロセスで再読取し、
セッション・時刻の包含/除外フィルター、未提供の `success` と `total_tokens` が
`null` のままであることを確認した。

### トークン照合の限界

| 記録 | input | output | cached input | cache write | reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: |
| 最初の使用量イベント | 11,719 | 0 | 0 | 0 | 0 |
| 次の使用量イベント | 14,651 | 45 | 11,520 | 0 | 0 |
| 最後の使用量イベント | 14,719 | 10 | 14,464 | 0 | 0 |
| CLI の turn.completed | 29,370 | 55 | 25,984 | 0 | 0 |

後の 2 イベントの合計は CLI の最終使用量に一致した。一方、最初の入力のみの
イベントも実際に受信されており、全イベントの input 合計は CLI と一致しない。
そのイベントの発生理由は未確認。先頭を常に捨てるなどの推測ルールは実装していない。
この基盤が保証するのはイベント単位の保存であり、請求額や CLI 合計の自動再構成ではない。

## 未検証・対象外

- 実 API エラー、失敗したツール、承認拒否のログ。合成入力の成否変換・保存は試験済み。
- `codex.api_request` と `codex.websocket_event` の実受信。今回の通信経路では未観測。
- 非ゼロの reasoning / cache-write と upstream 提供の total。合成データの変換だけを確認。
- 他の Codex 版、Python 3.11〜3.13、Windows、長時間・大量データ運用。
- upstream 再送、プロセス強制終了、実ディスク故障時の復旧。SQLite 障害応答と rollback は合成試験。
- 常駐化、ネットワーク公開、GitHub 配布、Codex インストール型プラグイン。

受信サービスは実通信検証後に停止した。これらの未確認項目を合成テストで実対応済みとは扱わない。
