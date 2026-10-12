package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"testing/fstest"
)

// fakeAdapterFiles stand in for the adapter; the real files are read from the repository below.
func fakeAdapterFiles(version string) fstest.MapFS {
	files := fstest.MapFS{}
	for _, name := range adapterFiles {
		files[name] = &fstest.MapFile{Data: []byte("# " + name + " " + version + "\n")}
	}
	return files
}

func loadAdapter(t *testing.T, fsys fstest.MapFS) *HostAdapter {
	t.Helper()
	adapter, err := LoadHostAdapter(fsys)
	if err != nil {
		t.Fatal(err)
	}
	return adapter
}

// runOnHost runs the adapter's installation the way SSM does, with sh, against directory, and answers its
// standard output.
func runOnHost(t *testing.T, adapter *HostAdapter, directory string) (string, error) {
	t.Helper()
	for _, tool := range []string{"sh", "base64", "gzip", "sha256sum", "mktemp", "stat"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("%s is not installed", tool)
		}
	}
	command := exec.Command("sh", "-c", "set -eu\n"+adapter.script(directory, strconv.Itoa(os.Getuid())))
	var stdout, stderr bytes.Buffer
	command.Stdout, command.Stderr = &stdout, &stderr
	err := command.Run()
	if err != nil {
		t.Logf("stderr: %s", stderr.String())
	}
	return stdout.String(), err
}

func inode(t *testing.T, path string) uint64 {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Sys().(*syscall.Stat_t).Ino
}

func TestTheRepositoryAdapterLoadsAndFitsOneCommand(t *testing.T) {
	adapter, err := LoadHostAdapter(os.DirFS(filepath.Join("..", "..", "scripts")))
	if err != nil {
		t.Fatal(err)
	}
	script := adapter.Script()
	for _, name := range adapterFiles {
		content, err := os.ReadFile(filepath.Join("..", "..", "scripts", name))
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(content)
		if !strings.Contains(script, "'"+hex.EncodeToString(sum[:])+"  "+name+"'") {
			t.Fatalf("the script does not pin %s", name)
		}
	}
	// SSM carries the whole script in one Run Command parameter; issue #55's adapter bundle of about this
	// size went through it. Keep well clear of any limit as the adapter grows.
	if len(script) > 48<<10 {
		t.Fatalf("the adapter script is %d bytes", len(script))
	}
}

func TestLoadHostAdapterNeedsEveryFile(t *testing.T) {
	files := fakeAdapterFiles("v1")
	delete(files, "minecraft_access.py")
	if _, err := LoadHostAdapter(files); err == nil {
		t.Fatal("a missing adapter file must fail the launcher's start-up")
	}
	files = fakeAdapterFiles("v1")
	files["minecraft_crafty.py"].Data = nil
	if _, err := LoadHostAdapter(files); err == nil {
		t.Fatal("an empty adapter file must fail the launcher's start-up")
	}
}

func TestAdapterInstallReplacesADifferentAdapterAndPrintsNothing(t *testing.T) {
	root := t.TempDir()
	directory := filepath.Join(root, "scripts")
	if err := os.Mkdir(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	old := fakeAdapterFiles("v1")
	for name, file := range old {
		if err := os.WriteFile(filepath.Join(directory, name), file.Data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(directory, "minecraft_other.py"), []byte("kept"), 0o644); err != nil {
		t.Fatal(err)
	}
	updated := fakeAdapterFiles("v2")
	stdout, err := runOnHost(t, loadAdapter(t, updated), directory)
	if err != nil || stdout != "" {
		t.Fatalf("stdout=%q err=%v", stdout, err)
	}
	for name, file := range updated {
		path := filepath.Join(directory, name)
		content, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(content, file.Data) {
			t.Fatalf("%s holds %q (%v)", name, content, err)
		}
		if info, _ := os.Stat(path); info.Mode().Perm() != 0o644 {
			t.Fatalf("%s has mode %v", name, info.Mode().Perm())
		}
	}
	if content, _ := os.ReadFile(filepath.Join(directory, "minecraft_other.py")); string(content) != "kept" {
		t.Fatal("the install must touch only the adapter's own files")
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 1 {
		t.Fatalf("the install left %v beside the adapter", entries)
	}
}

func TestAdapterInstallLeavesAMatchingAdapterAlone(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "scripts")
	if err := os.Mkdir(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	files := fakeAdapterFiles("v1")
	for name, file := range files {
		if err := os.WriteFile(filepath.Join(directory, name), file.Data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	before := inode(t, filepath.Join(directory, "minecraft_crafty.py"))
	if stdout, err := runOnHost(t, loadAdapter(t, files), directory); err != nil || stdout != "" {
		t.Fatalf("stdout=%q err=%v", stdout, err)
	}
	if inode(t, filepath.Join(directory, "minecraft_crafty.py")) != before {
		t.Fatal("a matching adapter must not be replaced")
	}
}

func TestAdapterInstallReplacesMatchingFilesSomeoneElseCouldChange(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "scripts")
	if err := os.Mkdir(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	files := fakeAdapterFiles("v1")
	for name, file := range files {
		if err := os.WriteFile(filepath.Join(directory, name), file.Data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// The right bytes, but group-writable, and one of them reached through a link: root must not run either.
	writable := filepath.Join(directory, "minecraft_crafty.py")
	if err := os.Chmod(writable, 0o664); err != nil {
		t.Fatal(err)
	}
	linked := filepath.Join(directory, "minecraft_access.py")
	elsewhere := filepath.Join(t.TempDir(), "minecraft_access.py")
	if err := os.Rename(linked, elsewhere); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(elsewhere, linked); err != nil {
		t.Fatal(err)
	}
	if stdout, err := runOnHost(t, loadAdapter(t, files), directory); err != nil || stdout != "" {
		t.Fatalf("stdout=%q err=%v", stdout, err)
	}
	for _, path := range []string{writable, linked} {
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o644 {
			t.Fatalf("%s was left as %v (%v)", path, info.Mode(), err)
		}
	}
}

func TestAdapterInstallRefusesAFolderSomeoneElseCouldChange(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "scripts")
	if err := os.Mkdir(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0o775); err != nil {
		t.Fatal(err)
	}
	if _, err := runOnHost(t, loadAdapter(t, fakeAdapterFiles("v1")), directory); err == nil {
		t.Fatal("a group-writable adapter folder must fail the command")
	}
	if entries, _ := os.ReadDir(directory); len(entries) != 0 {
		t.Fatalf("nothing may be installed into it: %v", entries)
	}
}

func TestAdapterInstallFailsWithoutTheCraftyInstallOrOnACorruptCopy(t *testing.T) {
	if _, err := runOnHost(t, loadAdapter(t, fakeAdapterFiles("v1")), filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Fatal("a host without the Crafty adapter folder must fail the command")
	}
	directory := filepath.Join(t.TempDir(), "scripts")
	if err := os.Mkdir(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(directory, "minecraft_content.py"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := runOnHost(t, loadAdapter(t, fakeAdapterFiles("v2")), directory); err == nil {
		t.Fatal("a directory in place of an adapter file must fail the command, not take the file inside it")
	}
	if info, err := os.Stat(filepath.Join(directory, "minecraft_content.py")); err != nil || !info.IsDir() {
		t.Fatalf("the directory must be left for an operator to look at: %v", err)
	}
	if err := os.Remove(filepath.Join(directory, "minecraft_content.py")); err != nil {
		t.Fatal(err)
	}
	for _, name := range adapterFiles {
		_ = os.Remove(filepath.Join(directory, name))
	}
	adapter := loadAdapter(t, fakeAdapterFiles("v2"))
	adapter.sums[2] = strings.Repeat("0", 64) // what arrives does not match what the launcher pinned
	if _, err := runOnHost(t, adapter, directory); err == nil {
		t.Fatal("a copy that fails its checksum must fail the command")
	}
	if entries, _ := os.ReadDir(directory); len(entries) != 0 {
		t.Fatalf("a failed install installed %v", entries)
	}
}

func TestCommandsRunAfterTheAdapterInstall(t *testing.T) {
	r := finishRig(t)
	r.launcher.Adapter = loadAdapter(t, fakeAdapterFiles("v1"))
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatal(err)
	}
	install := r.launcher.Adapter.Script()
	want := "set -eu\n" + install + "\n" + remoteCommand + " start family"
	if len(r.ssm.commands) != 1 || r.ssm.commands[0] != want {
		t.Fatalf("commands %q", r.ssm.commands)
	}

	a, _ := allowlistRig(t)
	a.launcher.Adapter = r.launcher.Adapter
	if _, err := a.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"}); err != nil {
		t.Fatal(err)
	}
	script := a.ssm.commands[0]
	if !strings.HasPrefix(script, "set -eu\n"+install+"\n") || strings.Index(script, `player="$(mktemp)"`) < len(install) {
		t.Fatalf("the allowlist must run after the install:\n%s", script)
	}
}
