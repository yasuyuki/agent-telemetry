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

29 テスト合格（Claude 追加後の全件再実行）。合成 OTLP の属性変換・欠損・ゼロ・未知イベント・不正入力・
nanosecond 時刻保持、SQLite ファイルへの本文非混入、再読取と時刻検索、
並行 HTTP 保存、transaction 途中の SQL 失敗で全件 rollback、保存失敗の 503、
HTTP framing 拒否、loopback 制限、既存の異種 DB を変更しないことを確認した。

`test_plugin.py` は一時 venv にコアと独立 fixture パッケージをインストールし、
repo 外の cwd、Python `-I` で実行する。インストール先からの import と、
entry point で選んだ別プラットフォームと `claude` の HTTP 200・SQLite 保存、
停止後の read-only 再読取を確認する。
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

## Claude Code 実通信

2026-09-08、Linux / Python 3.14.4 / Claude Code 2.1.263 で実施。
既存公開 `place.py list` で W1 の Claude を確認し、`place.py start` で起動した。
README の環境変数を試験プロセスだけに渡し、恒久設定・認証 HOME は変更しなかった。
CLI 本体の更新や配置変更は検証対象に含めない。
受信はインストール済み `agent-telemetry serve --adapter claude` を使用。
無害な `printf telemetry-smoke-ok` を Bash で1回実行し、CLI 終了成功・承認拒否なしと
OTel の `tool_result.success=true` を確認した。

最終検証セッションで6行を保存した（保存名はすべて `claude_code.*`）。

| イベント | 行数 | 確認した情報 |
| --- | ---: | --- |
| user_prompt | 1 | セッション ID と発生時刻、本文は保存しない |
| api_request | 3 | モデル、所要時間、入出力・キャッシュ読み書きトークン |
| tool_decision | 1 | Bash、success は null |
| tool_result | 1 | Bash、success=true、63 ms |

全行のセッション ID が CLI の結果と一致し、発生時刻は受信時刻以前だった。
CLI 版は全行で `2.1.263`。`app.version` だけを読む初回試験では欠損だったため、
[公式 service information](https://code.claude.com/docs/en/monitoring-usage#service-information)
の resource `service.version` を補完元に追加し、新セッションで再検証した。
Desktop アプリ版との混同を避け、resource `service.name=claude-code` の場合だけ補完する。
この優先順位と Desktop 除外は合成テストでも検証した。

API イベントには `claude-haiku-4-5-20251001` と `claude-opus-5` が含まれた。
非ゼロの入出力・cache-read・cache-write とゼロのキャッシュ値を観測した。
API イベントと tool_decision の success、全行の total / reasoning は null のまま。
CLI の使用量合計や課金額の再現は検証していない。

受信サービスを停止し、別プロセスのインストール済み query で6行を再読取した。
セッション検索、時刻の包含・除外、本文マーカーの SQLite ファイル非混入を確認した。
DB と CLI の試験結果は repo 外のローカルデータ領域に保存し、生 OTLP は記録しない。

合成試験では5イベントの正規化、属性対応、欠損・ゼロ・false、ナノ秒時刻、
不正な構造・値（巨大数を含む）、未知イベントと本文の非保存、バッチ末尾の不正入力で
HTTP 400・部分保存なしを確認した。共通 SQLite の SQL 途中失敗 rollback も全件再実行した。
実 API エラー、失敗ツール、承認拒否、Desktop、他の Claude 版は未検証。

## 未検証・対象外

- 実 API エラー、失敗したツール、承認拒否のログ。合成入力の成否変換・保存は試験済み。
- `codex.api_request` と `codex.websocket_event` の実受信。今回の通信経路では未観測。
- 非ゼロの reasoning / cache-write と upstream 提供の total。合成データの変換だけを確認。
- 他の Codex 版、Python 3.11〜3.13、Windows、長時間・大量データ運用。
- upstream 再送、プロセス強制終了、実ディスク故障時の復旧。SQLite 障害応答と rollback は合成試験。
- 常駐化、ネットワーク公開、GitHub 配布、Codex インストール型プラグイン。

受信サービスは実通信検証後に停止した。これらの未確認項目を合成テストで実対応済みとは扱わない。
