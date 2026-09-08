# agent-telemetry

Linux 向けのローカル実行記録基盤。Codex / Claude Code の公式 OpenTelemetry ログを
OTLP/HTTP JSON で受信し、許可した項目だけを SQLite へ保存する。
Python 3.11 以上。実行時の外部ライブラリは不要。

初版の対象は手動起動の受信サービスと JSON 検索。常駐化、Windows 配置、
GitHub 公開、Codex にインストールするプラグイン、メトリクス・トレース受信は対象外。
Python の入力アダプターは外部パッケージで追加できる。

## インストールと起動

Linux の Bash で、取得したこのリポジトリを作業ディレクトリにする。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
```

DB はリポジトリの外へ置く。次は本人のローカルデータ領域を使う例。
リクエスト上限と読み取りタイムアウトは運用側が明示する。
この例の 8 MiB / 10 秒は検証用の設定であり、Codex の保証上限ではない。

```bash
telemetry_dir="$HOME/.local/share/agent-telemetry"
install -d -m 700 "$telemetry_dir"
umask 077
serve=(.venv/bin/agent-telemetry serve)
serve+=(--db "$telemetry_dir/events.sqlite3")
serve+=(--host 127.0.0.1 --port 4318)
serve+=(--max-body-bytes 8388608 --read-timeout 10)
"${serve[@]}"
```

起動後はフォアグラウンドで待ち受ける。停止は Ctrl+C。
待受先は IP リテラルの loopback に限定し、DNS 名や外部インターフェースは拒否する。
HTTP の `POST /v1/logs`、`Content-Type: application/json`、単一の `Content-Length`
を受け付ける。圧縮、chunked 転送、他の OTLP エンコーディングは未対応。
localhost の他プロセスを認証する機能はないため、信頼できる単一利用者の環境で使う。

## Codex の設定

[公式 Advanced Configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
のログ exporter と、[Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)
の `protocol = "json"` を使う。2026-09-08 に確認。
公式説明ではログは非同期にバッチ送信され、終了時にフラッシュされる。

継続して利用する場合のユーザー設定例（`~/.codex/config.toml`）。
既存の `[otel]` がある場合は同じ表の項目を更新する。

```toml
[otel]
log_user_prompt = false
trace_exporter = "none"
metrics_exporter = "none"

[otel.exporter.otlp-http]
endpoint = "http://127.0.0.1:4318/v1/logs"
protocol = "json"
```

試験だけなら設定ファイルを変更せず、別の Bash ターミナルで引数を渡せる。
認証済み Codex の通常の起動入口を使う。公開 launcher を使う環境では、その入口の
CLI 引数部分へ次の `codex_args` を渡す。

```bash
codex_args=(exec --ephemeral --json)
codex_args+=(-c 'otel.log_user_prompt=false')
codex_args+=(-c 'otel.trace_exporter="none"')
codex_args+=(-c 'otel.metrics_exporter="none"')
otel_http='{endpoint="http://127.0.0.1:4318/v1/logs",protocol="json"}'
codex_args+=(-c "otel.exporter={otlp-http=$otel_http}")
codex_args+=('Run printf telemetry-smoke-ok once, then reply done. Do not modify files.')
codex "${codex_args[@]}"
```

ツールの成功と `turn.completed` が出て Codex が終了するまで待つ。
受信サービスも起動したままにし、次の検索で記録を確認する。
`log_user_prompt=false` でも upstream のツールログには本文が含まれ得る。
本サービスは body、prompt、arguments、output、error、user.email などを保存しない。
この設定は Codex 自身の履歴や別の送信先のログを削除するものではない。

## Claude Code の設定

受信サービスの起動引数に `--adapter claude` を追加する（既定は `codex`）。
1つの受信口は1つのアダプター専用。同時利用する場合は別ポートで起動する。
[Claude Code 公式 Monitoring](https://code.claude.com/docs/en/monitoring-usage)
のログ専用設定を使う（2026-09-08 確認）。以下は Bash の試験プロセスだけに適用し、
ユーザー設定ファイルは変更しない。受信サービスを先に起動しておく。

```bash
claude_env=(CLAUDE_CODE_ENABLE_TELEMETRY=1)
claude_env+=(OTEL_LOGS_EXPORTER=otlp)
claude_env+=(OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/json)
claude_env+=(OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs)
claude_env+=(OTEL_METRICS_EXPORTER=none OTEL_TRACES_EXPORTER=none)
claude_env+=(CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=0)
claude_env+=(ENABLE_ENHANCED_TELEMETRY_BETA=0)
claude_env+=(OTEL_LOG_USER_PROMPTS=0 OTEL_LOG_ASSISTANT_RESPONSES=0)
claude_env+=(OTEL_LOG_TOOL_DETAILS=0 OTEL_LOG_TOOL_CONTENT=0)
claude_env+=(OTEL_LOG_RAW_API_BODIES=0)
claude_args=(-p)
claude_args+=('Run printf telemetry-smoke-ok once, then reply done.')
claude_args+=(--tools Bash --allowedTools 'Bash(printf telemetry-smoke-ok)')
claude_args+=(--no-session-persistence --output-format json)
env "${claude_env[@]}" claude "${claude_args[@]}"
```

公開 launcher を使う環境では `claude` の部分を既存の公開起動入口に置き換え、
その CLI 引数部分へ `claude_args` を渡す。環境の managed 設定が送信先を固定している
場合はその設定が優先される。実行が終了してログが送信された後、検索結果の
`session_id` で絞り込む。受信サービス停止後も同じ query を使える。
本文出力を無効にしても利用者属性などは送られ得るが、本サービスは保存しない。

| 共通項目 | Claude OTLP の入力 |
| --- | --- |
| `platform` | `claude` |
| `event_name` | 属性 `event.name` に `claude_code.` を付与（既に付いていれば保持） |
| `session_id` | `session.id`（resource 属性も参照） |
| `model`, `cli_version` | `model`, `app.version`（resource 属性も参照）。版がなければ resource の `service.name=claude-code` に限り `service.version` |
| `occurred_at_ns` | 非ゼロの `timeUnixNano`、なければ `event.timestamp` |
| `tool_name`, `success`, `duration_ms` | 同名のイベント属性 |
| `input_tokens`, `output_tokens` | 同名のイベント属性 |
| `cached_input_tokens`, `cache_write_tokens` | `cache_read_tokens`, `cache_creation_tokens` |
| `reasoning_output_tokens`, `total_tokens` | 未提供のため常に `null` |

対象は `user_prompt`、`api_request`、`api_error`、`tool_decision`、`tool_result` のみ。
成否は明示された `success` だけを読み、API エラー名やツール承認判断から補完しない。
未提供値は `null`、提供されたゼロ値は `0`。OTLP の `timeUnixNano=0` は未設定を表す。
費用、本文、引数、出力、エラー文章、利用者情報、未知イベントは保存しない。

## 検索

このリポジトリの Bash ターミナルで実行する。出力は JSON 配列。
欠損値は `null`、提供されたゼロ値は `0` のまま保持する。

```bash
telemetry_db="$HOME/.local/share/agent-telemetry/events.sqlite3"
.venv/bin/agent-telemetry query --db "$telemetry_db"
```

セッション ID は Codex の `thread.started.thread_id` または検索結果の `session_id`。
以下の ID を対象セッションへ置き換える。

```bash
session_id='00000000-0000-0000-0000-000000000000'
query=(.venv/bin/agent-telemetry query --db "$telemetry_db")
query+=(--session "$session_id")
"${query[@]}"
```

`--since-ns` と `--until-ns` は UTC Unix epoch からの整数ナノ秒。
発生時刻で `since <= occurred_at_ns < until` に絞る。
時刻のないレコードは時刻フィルター付き検索に含まれない。
フィルターなしでは全件を返し、発生時刻・挿入順で並べる。
検索は DB を読み取り専用で開き、存在しない DB を新規作成しない。

## 保存仕様

固定の `Record`（スキーマ版 1）を `records` 表へ保存する。
`session_id, occurred_at_ns` と `occurred_at_ns` に索引がある。
文字列の識別子は英数字と `_.:/+-` のみとし、空白や `@` を含む任意識別子は保存しない。
組み込みアダプターでは該当値を欠損へ落とす。
これは許可項目への射影であり、任意の入力に対する秘密検出器ではない。
セッション ID と利用時刻も利用者の行動情報としてローカルで管理する。

| 共通項目 | Codex OTLP の入力 |
| --- | --- |
| `schema_version`, `platform` | 契約版 `1`、アダプター ID `codex` |
| `event_name` | 属性 `event.name`（下記既知イベントのみ） |
| `occurred_at_ns` | 非ゼロの `timeUnixNano`、なければ属性 `event.timestamp` の RFC 3339 時刻 |
| `received_at_ns` | 受信 body の読み取り完了時のサーバー UTC 時刻 |
| `session_id` | `conversation.id` |
| `model`, `cli_version` | `model`, `app.version`（resource 属性も参照） |
| `tool_name`, `success`, `duration_ms` | 同名のイベント属性 |
| `input_tokens`, `output_tokens` | `input_token_count`, `output_token_count` |
| `cached_input_tokens`, `cache_write_tokens` | `cached_token_count`, `cache_write_token_count` |
| `reasoning_output_tokens`, `total_tokens` | `reasoning_token_count`, `total_token_count` |

既知イベントは `codex.conversation_starts`、`codex.api_request`、`codex.sse_event`、
`codex.websocket_request`、`codex.websocket_event`、`codex.user_prompt`、
`codex.tool_decision`、`codex.tool_result`。未知イベントと未許可属性は捨てる。
ログ本文はイベント名の代替にも使わない。API のステータスから成否を推測せず、
トークン合計を計算せず、受信時刻を発生時刻の代わりにしない。
標準時刻と Codex 属性が両方ある場合は標準時刻を優先する。

1 HTTP バッチ全体を変換・検証し、1 SQLite transaction で保存する。
commit 完了後にだけ HTTP 200 と OTLP の空応答 `{}` を返す。
不正入力は 400、過大な body は 413、未対応形式は 415、保存障害は 503。
バッチ内に不正レコードがあれば、そのバッチを部分保存しない。
未知イベントだけの正常バッチは保存なしで成功する。
body、例外本文、HTTP アクセスログは記録しない。

commit 後の応答喪失や upstream の再送で重複する可能性がある。
exactly-once、強制終了時の未送信ログ回収、課金・請求額の再現は保証しない。
同じ操作で複数階層のツール結果が届くため、行数はユーザー操作数と一致しない。
トークン記録も CLI の最終合計に含まれないものがあり、単純合算を使用量とみなさない。
成功や total が未提供のイベントは欠損のまま残る。

## 外部入力プラグイン

HTTP、SQLite、CLI からプラットフォーム依存のデコードを切り離す契約が
`agent_telemetry.adapters.TelemetryAdapter`。
Python パッケージは `identifier: str` と
`decode(payload: bytes, received_at_ns: int) -> list[Record]` を公開する。
不正入力は `ValueError` にする。欠損の補完と自由文の格納は行わない。
プラグインは信頼済み Python コードとして実行され、sandbox ではない。
同じインスタンスが並行リクエストを扱うため、状態を持つ実装は同期が必要。

プラグイン側の `pyproject.toml` に登録する。キーと `identifier` は一致させる。

```toml
[project.entry-points."agent_telemetry.adapters"]
example = "example_adapter:ExampleAdapter"
```

```python
from agent_telemetry.model import Record

class ExampleAdapter:
    identifier = "example"

    def decode(self, payload: bytes, received_at_ns: int) -> list[Record]:
        if payload != b'{}':
            raise ValueError("unsupported input")
        return [Record(platform=self.identifier, event_name="example.ping",
                       received_at_ns=received_at_ns)]
```

同じ Python 環境へプラグインをインストールし、受信コマンドへ
`--adapter example` を渡す。エンドポイントは引き続き `/v1/logs`。
Codex 自身も同じ entry point 契約で登録している。重複した登録名は拒否する。
テスト用の独立パッケージは `tests/fixtures/external_plugin` にある。

## 検証

リポジトリ root で、インストール後に実行する。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

合成データで変換、欠損、未知イベント、不正入力、本文除外、時刻精度、
SQLite 再読取、並行 HTTP、保存障害・rollback、loopback と framing を検証する。
実通信の観測と未確認項目は [VERIFICATION.md](VERIFICATION.md) に分けて記載する。
合成テストの成功を未観測の Codex 版・OS・イベントの実対応証明には使わない。
