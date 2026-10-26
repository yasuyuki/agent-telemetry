# agent-telemetry

Codex / Claude Code の OTLP logs をローカルで受け、許可した項目だけを SQLite
schema v1 に保存する。受信と OTLP decode は OpenTelemetry Collector **0.161.0**、
保存はこの repository の小さな Collector exporter、検索は Python package が担当する。
Collector は手動の foreground 起動を想定する。外部 network、dashboard、daemon は不要。

## 固定版と構築

Linux amd64、Go 1.26.0、Python 3.14、C compiler が必要。
公式 `otelcol-contrib` 0.161.0 の stock probe に使用した archive
`otelcol-contrib_0.161.0_linux_amd64.tar.gz` の SHA-256 は
`778c689efa681ff6e4722ce9f66b9b7f57c3ba009ab2e2b43dc2e0315862c731`。
本番の binary は [Builder manifest](collector/builder.yaml) に固定した
OTLP receiver、transform processor、local exporter だけで構築する。
依存版は [exporter/go.mod](exporter/go.mod) と `go.sum` に固定する。
起動時に download しない。

repository root で:

```bash
go install go.opentelemetry.io/collector/cmd/builder@v0.161.0
cd collector
builder --config builder.yaml
```

成果物は `collector/build/agent-telemetry-collector`。source を別の location
へ置いても manifest の相対 `replace` で exporter を取り込む。
Python query wheel は repository root で `python3 -m pip wheel . -w dist --no-deps`
から作れる。package 版は `0.2.0`。

## 受信と検索

[config.yaml](collector/config.yaml) は OTLP/HTTP logs のみを
`127.0.0.1:4318` で待ち受ける。body 上限は 8 MiB、request read timeout は 10 秒。
gRPC、metrics / traces pipeline、batch、queue、retry、debug/file exporter はない。
`AGENT_TELEMETRY_PLATFORM` を `codex` または `claude` と明示し、
`AGENT_TELEMETRY_DB` には repository 外の絶対 SQLite path を設定する。
1 instance は1 platform 用。同時利用時は別 port の config と別 instance を使う。
DB と認証情報を repository に置かない。

```bash
export AGENT_TELEMETRY_PLATFORM=codex
export AGENT_TELEMETRY_DB="$HOME/.local/share/agent-telemetry/events.sqlite3"
collector/build/agent-telemetry-collector --config=collector/config.yaml
```

既存の Codex OTLP/HTTP JSON endpoint は
`http://127.0.0.1:4318/v1/logs` のままでよい。Claude Code は同じ endpoint に
OTLP logs を送れるが、起動する Collector の platform は `claude` にする。
old Python `serve` command はない。古い DB の一括変換も行わない。

query は DB を read-only で開き、DB がなければ作成しない。

```bash
agent-telemetry query --db "$AGENT_TELEMETRY_DB"
agent-telemetry query --db "$AGENT_TELEMETRY_DB" --session session-id
```

`--since-ns` は inclusive、`--until-ns` は exclusive。未設定の時刻は
時刻 filter に入らない。出力は全17項目の JSON 配列で、欠損は `null`、明示ゼロは
`0`、成功値は boolean。`occurred_at_ns, id` 順。`Record` と `Storage.query`
は既存 apparatus query consumer のため維持する。

## 保存境界

Collector の transform は永続化より前に log body、不要な log/resource/scope
属性、scope 名・版、未知イベント名を除去する。`error_mode: propagate` を明示し、
変換エラーを通過させない。exporter は sanitized な pdata logs だけを受け、
config で指定した platform の既知イベントを schema v1 `Record` へ正規化する。
任意の本文・引数・出力・エラー本文・利用者属性は DB や正常時の Collector log に
書かない。識別子は旧版と同じ文字集合制約で自由文を落とす。

1 HTTP request から渡る1 `ConsumeLogs` call の全既知 record を先に検証し、
1 SQLite transaction に保存する。commit 完了後だけ HTTP 成功となる。
不正な既知 record または保存障害は非成功で、partial row は残さない。
未知イベントだけなら row なしで成功する。queue 投入を保存済みとは扱わない。
commit 後の応答喪失と upstream retry では重複し得る。exactly-once、利用額の
再構成、強制終了前の upstream 未送信分は保証しない。

### stock store の採否

公式 0.161.0 配布には SQLite `dbstorage` **extension** はあるが、既存
schema v1 `records` 表へ同期 transaction で書く exporter はない。
stock file exporter の出力は既存 `Storage.query` が読む schema / index を
提供せず、queue は最終 query store への commit ではない。したがって
request atomicity、commit 後 ack、既存 query を同時に満たす stock store は
不採用とし、custom exporter を1つだけ持つ。
根拠は [公式 0.161.0 manifest](https://github.com/open-telemetry/opentelemetry-collector-releases/blob/v0.161.0/distributions/otelcol-contrib/manifest.yaml)
と [queue の契約](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.161.0/exporter/exporterhelper/README.md)。

## 検証

```bash
cd exporter
go test ./...
cd ..
python3 -m unittest discover -s tests -v
```

Collector 実 binary の統合試験は `AGENT_TELEMETRY_COLLECTOR` に上記 binary の
絶対 path を渡して同じ unittest を実行する。未設定時は該当試験を skip する。
CI は build 後に指定して実行する。合成 secret は入力を disk に保存せずに
送信し、DB、query、Collector stdout/stderr、生成ファイルへの混入を検査する。
