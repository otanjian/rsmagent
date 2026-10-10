// Run from a temporary Go module with github.com/moby/patternmatcher v0.6.0.
// This validates Docker's path matching, not image contents or history layers.
package main

import (
    "fmt"
    "os"
    "os/exec"
    "path/filepath"
    "strings"

    "github.com/moby/patternmatcher"
    "github.com/moby/patternmatcher/ignorefile"
)

func main() {
    root := os.Args[1]
    file, err := os.Open(filepath.Join(root, ".dockerignore"))
    if err != nil { panic(err) }
    defer file.Close()
    patterns, err := ignorefile.ReadAll(file)
    if err != nil { panic(err) }
    matcher, err := patternmatcher.New(patterns)
    if err != nil { panic(err) }
    assert := func(name string, excluded bool) {
        actual, err := matcher.MatchesOrParentMatches(name)
        if err != nil || actual != excluded {
            panic(fmt.Sprintf("unexpected exclusion for %s: excluded=%v error=%v", name, actual, err))
        }
    }
    forbidden := []string{
        "config.json", "config.json.bak-test", "identity.db", "identity.db-wal",
        ".preview_secret", ".env", "run.log", "run.log.1", ".git/config",
        "backups/instance.zip", "tenants/synthetic/private.txt", "branding/state.json",
        "memory/session.json", "tmp/output.txt", "docker/agent-mail-data/secrets.json",
        "agent/.env", "skills/example/config.json", "skills/example/private.key",
        "channel/web/cache.sqlite3", "plugins/tool/backups/secret.zip",
    }
    for _, name := range forbidden { assert(name, true) }
    for _, name := range []string{"app.py", "config.py", "config-template.json", "pyproject.toml",
        "requirements.txt", "requirements-optional.txt", "requirements-scenes.txt",
        "docker/entrypoint.sh", "docker/Dockerfile.latest", "webhelp/config.json"} {
        assert(name, false)
    }
    cmd := exec.Command("git", "ls-files", "-z")
    cmd.Dir = root
    tracked, err := cmd.Output()
    if err != nil { panic(err) }
    runtime := "|agent|auth|bridge|channel|cli|common|contracts|integrations|models|plugins|scenes|Scene|skills|tools|translate|voice|webhelp|"
    included := 0
    for _, name := range strings.Split(string(tracked), "\x00") {
        prefix, _, nested := strings.Cut(name, "/")
        if nested && strings.Contains(runtime, "|"+prefix+"|") {
            assert(name, false)
            included++
        }
    }
    fmt.Printf("PASS: %d forbidden paths excluded; %d tracked runtime files retained. Image layers NOT tested.\n", len(forbidden), included)
}
