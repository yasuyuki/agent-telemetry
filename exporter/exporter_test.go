package agenttelemetryexporter

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"go.opentelemetry.io/collector/consumer/consumererror"
	"go.opentelemetry.io/collector/pdata/plog"
)

func fixture(platform string) plog.Logs {
	l := plog.NewLogs()
	r := l.ResourceLogs().AppendEmpty()
	r.Resource().Attributes().PutStr("model", "model-1")
	r.Resource().Attributes().PutStr("app.version", "1.2")
	a := r.ScopeLogs().AppendEmpty().LogRecords().AppendEmpty().Attributes()
	a.PutStr("event.name", platform+".api_request")
	a.PutStr("event.timestamp", "2026-01-02T03:04:05.123456789Z")
	a.PutStr("tool_name", "bash")
	a.PutStr("success", "true")
	a.PutStr("duration_ms", "2.5")
	return l
}
func TestNormalize(t *testing.T) {
	l := fixture("codex")
	a := l.ResourceLogs().At(0).ScopeLogs().At(0).LogRecords().At(0).Attributes()
	a.PutStr("conversation.id", "session-1")
	a.PutStr("model", "override")
	a.PutStr("input_token_count", "42")
	a.PutInt("reasoning_token_count", 7)
	rows, e := normalize(l, "codex", 123)
	if e != nil {
		t.Fatal(e)
	}
	r := rows[0]
	if r[2] != "codex.api_request" || r[3] != int64(1767323045123456789) || r[4] != int64(123) || r[5] != "session-1" || r[6] != "override" || r[9] != true || r[10] != 2.5 || r[11] != int64(42) || r[15] != int64(7) {
		t.Fatalf("unexpected record: %#v", r)
	}
	a.PutStr("tool_name", "secret free text")
	rows, e = normalize(l, "codex", 123)
	if e != nil || rows[0][8] != nil {
		t.Fatal("unsafe identifier retained")
	}
	a.PutStr("tool_name", `C:\Users\Alice`)
	rows, e = normalize(l, "codex", 123)
	if e != nil || rows[0][8] != nil {
		t.Fatal("Windows path retained as identifier")
	}
}
func TestClaudeVersionAndTokens(t *testing.T) {
	l := fixture("claude_code")
	m := l.ResourceLogs().At(0).Resource().Attributes()
	m.Remove("app.version")
	m.PutStr("service.name", "claude-code")
	m.PutStr("service.version", "2.3")
	a := l.ResourceLogs().At(0).ScopeLogs().At(0).LogRecords().At(0).Attributes()
	a.PutInt("cache_read_tokens", 3)
	a.PutInt("cache_creation_tokens", 4)
	rows, e := normalize(l, "claude", 123)
	if e != nil {
		t.Fatal(e)
	}
	if rows[0][7] != "2.3" || rows[0][13] != int64(3) || rows[0][14] != int64(4) || rows[0][15] != nil {
		t.Fatal(rows)
	}
	m.PutStr("service.name", "claude-desktop")
	rows, e = normalize(l, "claude", 123)
	if e != nil || rows[0][7] != nil {
		t.Fatal("desktop version retained")
	}
}
func TestAtomicCommitAndRollback(t *testing.T) {
	ctx := context.Background()
	s := &sink{config: Config{Database: filepath.Join(t.TempDir(), "test.db"), Platform: "codex"}}
	if e := s.Start(ctx, nil); e != nil {
		t.Fatal(e)
	}
	defer s.Shutdown(ctx)
	l := fixture("codex")
	records := l.ResourceLogs().At(0).ScopeLogs().At(0).LogRecords()
	records.At(0).CopyTo(records.AppendEmpty())
	records.At(1).Attributes().PutStr("input_token_count", "secret-invalid")
	if e := s.ConsumeLogs(ctx, l); e == nil || !consumererror.IsPermanent(e) || strings.Contains(e.Error(), "secret") {
		t.Fatal("invalid request not safely rejected")
	}
	count := func() int {
		t.Helper()
		var n int
		if e := s.db.QueryRow("SELECT count(*) FROM records").Scan(&n); e != nil {
			t.Fatal(e)
		}
		return n
	}
	if count() != 0 {
		t.Fatal("partial commit")
	}
	records.At(1).Attributes().PutInt("input_token_count", 2)
	if e := s.ConsumeLogs(ctx, l); e != nil {
		t.Fatal(e)
	}
	if count() != 2 {
		t.Fatal("not committed before return")
	}
	// Force a failure on the second insert, after the first insert has executed.
	if _, e := s.db.Exec("CREATE TRIGGER reject_second BEFORE INSERT ON records WHEN NEW.input_tokens = 2 BEGIN SELECT RAISE(ABORT, 'private database detail'); END"); e != nil {
		t.Fatal(e)
	}
	if e := s.ConsumeLogs(ctx, l); e == nil || consumererror.IsPermanent(e) || strings.Contains(e.Error(), "private") {
		t.Fatal("write failure not safely rejected")
	}
	if count() != 2 {
		t.Fatal("failed transaction partly persisted")
	}
}
func TestUnknownAndInvalidValues(t *testing.T) {
	l := fixture("unknown")
	a := l.ResourceLogs().At(0).ScopeLogs().At(0).LogRecords().At(0).Attributes()
	a.PutStr("duration_ms", "private-invalid")
	if rows, e := normalize(l, "codex", 1); e != nil || len(rows) != 0 {
		t.Fatal("unknown event not ignored")
	}
	a.PutStr("event.name", "codex.api_request")
	for key, value := range map[string]string{"duration_ms": "NaN", "input_token_count": "9223372036854775808", "success": "yes", "event.timestamp": "1969-01-01T00:00:00Z"} {
		fresh := fixture("codex")
		fresh.ResourceLogs().At(0).ScopeLogs().At(0).LogRecords().At(0).Attributes().PutStr(key, value)
		if _, e := normalize(fresh, "codex", 1); e == nil {
			t.Errorf("accepted invalid %s", key)
		}
	}
}
func TestRejectIncompatibleSchema(t *testing.T) {
	ctx := context.Background()
	s := &sink{config: Config{Database: filepath.Join(t.TempDir(), "test.db"), Platform: "codex"}}
	if e := s.Start(ctx, nil); e != nil {
		t.Fatal(e)
	}
	if _, e := s.db.Exec("PRAGMA user_version=2"); e != nil {
		t.Fatal(e)
	}
	if e := s.ConsumeLogs(ctx, fixture("codex")); e == nil {
		t.Fatal("accepted changed schema during consume")
	}
	if e := s.ConsumeLogs(ctx, fixture("unknown")); e == nil {
		t.Fatal("accepted changed schema for unknown event")
	}
	s.Shutdown(ctx)
	if e := s.Start(ctx, nil); e == nil {
		t.Fatal("silently migrated schema")
	}
}

func TestDurableFilesystemDatabase(t *testing.T) {
	for _, path := range []string{"", ":memory:", "file:/tmp/test.db?mode=memory", "relative.db"} {
		if (&Config{Database: path, Platform: "codex"}).Validate() == nil {
			t.Fatal("accepted non-filesystem database")
		}
	}
	ctx := context.Background()
	// A URI-looking suffix is a literal filename, never connection options.
	s := &sink{config: Config{Database: filepath.Join(t.TempDir(), "literal?_synchronous=OFF.db"), Platform: "codex"}}
	if err := s.Start(ctx, nil); err != nil {
		t.Fatal(err)
	}
	defer s.Shutdown(ctx)
	var sync int
	var journal string
	if err := s.db.QueryRow("PRAGMA synchronous").Scan(&sync); err != nil || sync != 2 {
		t.Fatal("synchronous is not FULL")
	}
	if err := s.db.QueryRow("PRAGMA journal_mode").Scan(&journal); err != nil || journal != "delete" {
		t.Fatal("journal is not durable")
	}
}
