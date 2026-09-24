// Package agenttelemetryexporter stores sanitized agent log records synchronously.
package agenttelemetryexporter

import (
	"context"
	"database/sql"
	"errors"
	"math"
	"net/url"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/consumer/consumererror"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/plog"
)

// Config deliberately has no queue or retry options. Platform is never inferred.
type Config struct {
	Database string `mapstructure:"database"`
	Platform string `mapstructure:"platform"`
}

func (c *Config) Validate() error {
	if !filepath.IsAbs(c.Database) {
		return errors.New("database must be an absolute filesystem path")
	}
	if c.Platform != "codex" && c.Platform != "claude" {
		return errors.New("platform must be codex or claude")
	}
	return nil
}
func NewFactory() exporter.Factory {
	return exporter.NewFactory(component.MustNewType("agenttelemetry"), func() component.Config { return &Config{} }, exporter.WithLogs(func(_ context.Context, _ exporter.Settings, c component.Config) (exporter.Logs, error) {
		cfg := c.(*Config)
		if err := cfg.Validate(); err != nil {
			return nil, err
		}
		return &sink{config: *cfg}, nil
	}, component.StabilityLevelAlpha))
}

type sink struct {
	config Config
	db     *sql.DB
}

func (s *sink) Capabilities() consumer.Capabilities { return consumer.Capabilities{MutatesData: false} }
func (s *sink) Start(ctx context.Context, _ component.Host) error {
	if err := s.config.Validate(); err != nil {
		return err
	}
	// Encode the filesystem path so it cannot inject SQLite URI options. Apply
	// durability settings to every connection the pool might open.
	dsn := url.URL{Scheme: "file", Path: s.config.Database, RawQuery: "_synchronous=FULL&_journal_mode=DELETE"}
	db, err := sql.Open("sqlite3", dsn.String())
	if err != nil {
		return errors.New("database open failed")
	}
	db.SetMaxOpenConns(1)
	var synchronous int
	var journal string
	if db.QueryRowContext(ctx, "PRAGMA synchronous").Scan(&synchronous) != nil || synchronous != 2 || db.QueryRowContext(ctx, "PRAGMA journal_mode").Scan(&journal) != nil || journal != "delete" {
		db.Close()
		return errors.New("database durability configuration failed")
	}
	if err = initialize(ctx, db); err != nil {
		db.Close()
		return err
	}
	s.db = db
	return nil
}
func (s *sink) Shutdown(context.Context) error {
	if s.db != nil {
		if s.db.Close() != nil {
			return errors.New("database close failed")
		}
	}
	return nil
}
func (s *sink) ConsumeLogs(ctx context.Context, logs plog.Logs) error {
	records, err := normalize(logs, s.config.Platform, time.Now().UnixNano())
	if err != nil {
		return consumererror.NewPermanent(errors.New("invalid agent telemetry record"))
	}
	if s.db == nil {
		return errors.New("database is not started")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return errors.New("database transaction failed")
	}
	defer tx.Rollback()
	if err = validate(ctx, tx); err != nil {
		return err
	}
	for _, r := range records {
		if _, err = tx.ExecContext(ctx, insert, r...); err != nil {
			return errors.New("database write failed")
		}
	}
	if err = tx.Commit(); err != nil {
		return errors.New("database commit failed")
	}
	return nil
}

const columns = "schema_version, platform, event_name, occurred_at_ns, received_at_ns, session_id, model, cli_version, tool_name, success, duration_ms, input_tokens, output_tokens, cached_input_tokens, cache_write_tokens, reasoning_output_tokens, total_tokens"
const insert = "INSERT INTO records (" + columns + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
const create = `CREATE TABLE records (
 id INTEGER PRIMARY KEY,
 schema_version INTEGER NOT NULL, platform TEXT NOT NULL, event_name TEXT NOT NULL,
 occurred_at_ns INTEGER, received_at_ns INTEGER NOT NULL, session_id TEXT, model TEXT,
 cli_version TEXT, tool_name TEXT, success INTEGER, duration_ms REAL,
 input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, cache_write_tokens INTEGER,
 reasoning_output_tokens INTEGER, total_tokens INTEGER
)`

type queryer interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func validate(ctx context.Context, q queryer) error {
	bad := errors.New("incompatible database schema")
	var version int
	if q.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version) != nil || version != 1 {
		return bad
	}
	rows, err := q.QueryContext(ctx, "PRAGMA table_info(records)")
	if err != nil {
		return bad
	}
	defer rows.Close()
	expected := strings.Split("id, "+columns, ", ")
	i := 0
	for rows.Next() {
		var cid, nn, pk int
		var name, typ string
		var def any
		if rows.Scan(&cid, &name, &typ, &nn, &def, &pk) != nil || i >= len(expected) || name != expected[i] {
			return bad
		}
		i++
	}
	if rows.Err() != nil || i != len(expected) {
		return bad
	}
	return nil
}
func initialize(ctx context.Context, db *sql.DB) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return errors.New("database initialization failed")
	}
	defer tx.Rollback()
	var version, count int
	if tx.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version) != nil || tx.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_master").Scan(&count) != nil {
		return errors.New("database initialization failed")
	}
	if version == 0 && count == 0 {
		for _, stmt := range []string{create, "CREATE INDEX records_session_time ON records(session_id, occurred_at_ns)", "CREATE INDEX records_time ON records(occurred_at_ns)", "PRAGMA user_version=1"} {
			if _, err = tx.ExecContext(ctx, stmt); err != nil {
				return errors.New("database initialization failed")
			}
		}
	} else if err = validate(ctx, tx); err != nil {
		return err
	}
	if tx.Commit() != nil {
		return errors.New("database initialization failed")
	}
	return nil
}

var identifier = regexp.MustCompile(`^[A-Za-z0-9_.:/+-]+$`)
var digits = regexp.MustCompile(`^[0-9]+$`)
var timestamp = regexp.MustCompile(`^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)$`)
var invalid = errors.New("invalid value")

func scalar(m pcommon.Map, key string) (any, error) {
	v, ok := m.Get(key)
	if !ok {
		return nil, nil
	}
	switch v.Type() {
	case pcommon.ValueTypeStr:
		return v.Str(), nil
	case pcommon.ValueTypeBool:
		return v.Bool(), nil
	case pcommon.ValueTypeInt:
		if v.Int() >= 0 {
			return v.Int(), nil
		}
	case pcommon.ValueTypeDouble:
		if !math.IsNaN(v.Double()) && !math.IsInf(v.Double(), 0) {
			return v.Double(), nil
		}
	}
	return nil, invalid
}
func integer(v any) (any, error) {
	if v == nil {
		return nil, nil
	}
	switch n := v.(type) {
	case int64:
		if n >= 0 {
			return n, nil
		}
	case string:
		if digits.MatchString(n) {
			i, e := strconv.ParseInt(n, 10, 64)
			if e == nil {
				return i, nil
			}
		}
	}
	return nil, invalid
}
func ident(v any) any {
	if s, ok := v.(string); ok && identifier.MatchString(s) {
		return s
	}
	return nil
}
func duration(v any) (any, error) {
	if v == nil {
		return nil, nil
	}
	var n float64
	switch x := v.(type) {
	case int64:
		n = float64(x)
	case float64:
		n = x
	case string:
		var e error
		n, e = strconv.ParseFloat(strings.TrimSpace(x), 64)
		if e != nil {
			return nil, invalid
		}
	default:
		return nil, invalid
	}
	if n < 0 || math.IsNaN(n) || math.IsInf(n, 0) {
		return nil, invalid
	}
	return n, nil
}
func success(v any) (any, error) {
	switch x := v.(type) {
	case nil:
		return nil, nil
	case bool:
		return x, nil
	case string:
		if x == "true" {
			return true, nil
		}
		if x == "false" {
			return false, nil
		}
	}
	return nil, invalid
}
func occurred(l plog.LogRecord) (any, error) {
	if l.Timestamp() != 0 {
		if uint64(l.Timestamp()) > math.MaxInt64 {
			return nil, invalid
		}
		return int64(l.Timestamp()), nil
	}
	v, e := scalar(l.Attributes(), "event.timestamp")
	if e != nil || v == nil {
		return nil, e
	}
	s, ok := v.(string)
	if !ok || !timestamp.MatchString(s) {
		return nil, invalid
	}
	t, e := time.Parse(time.RFC3339Nano, s)
	if e != nil || t.Before(time.Unix(0, 0)) || t.After(time.Unix(0, math.MaxInt64)) {
		return nil, invalid
	}
	return t.UnixNano(), nil
}
func normalize(logs plog.Logs, platform string, received int64) ([][]any, error) {
	var out [][]any
	for _, rl := range logs.ResourceLogs().All() {
		for _, sl := range rl.ScopeLogs().All() {
			for _, l := range sl.LogRecords().All() {
				a, m := l.Attributes(), rl.Resource().Attributes()
				v, e := scalar(a, "event.name")
				if e != nil {
					return nil, e
				}
				event, ok := v.(string)
				if !ok {
					continue
				}
				if platform == "claude" {
					event = strings.TrimPrefix(event, "claude_code.")
					switch event {
					case "user_prompt", "api_request", "api_error", "tool_decision", "tool_result":
						event = "claude_code." + event
					default:
						continue
					}
				} else {
					switch event {
					case "codex.conversation_starts", "codex.api_request", "codex.sse_event", "codex.websocket_request", "codex.websocket_event", "codex.user_prompt", "codex.tool_decision", "codex.tool_result":
					default:
						continue
					}
				}
				when, e := occurred(l)
				if e != nil {
					return nil, e
				}
				r := make([]any, 17)
				r[0], r[1], r[2], r[3], r[4] = 1, platform, event, when, received
				session := "conversation.id"
				if platform == "claude" {
					session = "session.id"
				}
				for i, key := range []string{session, "model", "app.version"} {
					source := m
					if _, ok := a.Get(key); ok {
						source = a
					}
					v, e = scalar(source, key)
					if e != nil {
						return nil, e
					}
					if i == 2 && v == nil && platform == "claude" {
						name, err := scalar(m, "service.name")
						if err != nil {
							return nil, err
						}
						if name == "claude-code" {
							v, e = scalar(m, "service.version")
							if e != nil {
								return nil, e
							}
						}
					}
					r[5+i] = ident(v)
				}
				v, e = scalar(a, "tool_name")
				if e != nil {
					return nil, e
				}
				r[8] = ident(v)
				v, e = scalar(a, "success")
				if e != nil {
					return nil, e
				}
				r[9], e = success(v)
				if e != nil {
					return nil, e
				}
				v, e = scalar(a, "duration_ms")
				if e != nil {
					return nil, e
				}
				r[10], e = duration(v)
				if e != nil {
					return nil, e
				}
				keys := []string{"input_token_count", "output_token_count", "cached_token_count", "cache_write_token_count", "reasoning_token_count", "total_token_count"}
				if platform == "claude" {
					keys = []string{"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}
				}
				for i, key := range keys {
					v, e = scalar(a, key)
					if e != nil {
						return nil, e
					}
					r[11+i], e = integer(v)
					if e != nil {
						return nil, e
					}
				}
				out = append(out, r)
			}
		}
	}
	return out, nil
}
