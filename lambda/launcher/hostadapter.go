package main

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io/fs"
	"strings"
)

// The launcher carries the host adapter it calls, the Crafty adapter installed on the host as
// /usr/local/sbin/minecraft-instance, and installs it before each game start and allowlist change whenever
// the host's copy differs. A deploy of the launcher therefore always brings the adapter its commands were
// written for (ADR 0007). The package script puts the files in the function's adapter folder.
const adapterDirectory = "/opt/minecraft-crafty/scripts"

// adapterFiles are the files scripts/install_crafty.sh puts in adapterDirectory, in the order they are swapped
// in: the modules the adapter imports and the pinned server content it applies on each start (ADR 0008) first, the
// adapter itself last. scripts/package_idle_stop.py packs the same list, which tests/test_launcher_package.py checks.
var adapterFiles = []string{"minecraft_access.py", "minecraft_content.py", "server.json", "minecraft_crafty.py"}

// checkoutPaths name where a checkout keeps the adapter files that are not in its scripts folder.
var checkoutPaths = map[string]string{"server.json": "runtime/crafty/server.json"}

// checkout reads the adapter files from a checkout's root: the scripts folder, and the pinned server content
// where checkoutPaths name it.
type checkout struct{ root fs.FS }

func (c checkout) Open(name string) (fs.File, error) {
	if path, ok := checkoutPaths[name]; ok {
		return c.root.Open(path)
	}
	return c.root.Open("scripts/" + name)
}

// HostAdapter holds the adapter files this launcher was deployed with.
type HostAdapter struct {
	names   []string
	sums    []string
	encoded []string
}

// LoadHostAdapter reads the adapter files from fsys.
func LoadHostAdapter(fsys fs.FS) (*HostAdapter, error) {
	adapter := &HostAdapter{}
	for _, name := range adapterFiles {
		content, err := fs.ReadFile(fsys, name)
		if err != nil {
			return nil, fmt.Errorf("read host adapter file %s: %w", name, err)
		}
		if len(content) == 0 {
			return nil, fmt.Errorf("host adapter file %s is empty", name)
		}
		sum := sha256.Sum256(content)
		var packed bytes.Buffer
		writer, _ := gzip.NewWriterLevel(&packed, gzip.BestCompression)
		if _, err := writer.Write(content); err != nil {
			return nil, err
		}
		if err := writer.Close(); err != nil {
			return nil, err
		}
		adapter.names = append(adapter.names, name)
		adapter.sums = append(adapter.sums, hex.EncodeToString(sum[:]))
		adapter.encoded = append(adapter.encoded, base64.StdEncoding.EncodeToString(packed.Bytes()))
	}
	return adapter, nil
}

// Script answers shell lines, for a script already running under `set -eu`, that leave the host's adapter
// exactly as this launcher carries it. Root runs the adapter, so its folder must be root's and writable by
// nobody else, and no folder above it may let anyone else rename it, or the command fails. A host whose files
// already match, as root's own regular files that
// nobody else may write, changes nothing. Otherwise the files are unpacked beside the installed ones, checked
// against their SHA-256, renamed into place one at a time and checked once more where they landed.
// Each rename is atomic, so an adapter command never reads a partly written file. The lines print nothing to
// standard output, which stays the following command's answer.
func (a *HostAdapter) Script() string {
	return a.script(adapterDirectory, "0")
}

// script installs into directory, whose files and the folder itself must belong to the user ID owner, and
// every folder above it to owner or root.
func (a *HostAdapter) script(directory, owner string) string {
	if a == nil {
		return ""
	}
	sums := "printf '%s\\n'"
	for i, name := range a.names {
		sums += " '" + a.sums[i] + "  " + name + "'"
	}
	names := strings.Join(a.names, " ")
	lines := []string{
		"adapter=" + directory,
		`test -d "$adapter" || { echo 'the Crafty adapter is not installed on this host' >&2; exit 1; }`,
		// guarded KIND PATH...: each path is a KIND (stat's %F, never a link) owned by owner, that neither its
		// group nor others may write.
		`guarded() { kind="$1"; shift; for path; do [ "$(stat -c '%F:%u' -- "$path")" = "$kind:` + owner + `" ] || return 1; case "$(stat -c %A -- "$path")" in ?????w????|????????w?) return 1 ;; esac; done; }`,
		// above DIR: DIR and each folder above it belong to owner or root, and neither its group nor others may
		// write it, unless its sticky bit keeps them from renaming what they do not own.
		`above() { dir="$1"; while :; do case "$(stat -c '%u' -- "$dir")" in 0|` + owner + `) ;; *) return 1 ;; esac; case "$(stat -c %A -- "$dir")" in d????????[tT]) ;; d????w????|d???????w?) return 1 ;; d*) ;; *) return 1 ;; esac; [ "$dir" = / ] && return 0; dir="$(dirname -- "$dir")"; done; }`,
		`guarded directory "$adapter" && above "$(dirname -- "$adapter")" || { echo 'the Crafty adapter folder may be changed by someone other than root' >&2; exit 1; }`,
		`if ! (cd "$adapter" && guarded 'regular file' ` + names + ` && ` + sums + ` | sha256sum --check --status --strict) 2>/dev/null; then`,
		"(",
		`staging="$(mktemp -d "$adapter/../.adapter.XXXXXX")"`,
		`trap 'rm -rf "$staging"' EXIT`,
	}
	for i, name := range a.names {
		lines = append(lines, "printf '%s' '"+a.encoded[i]+`' | base64 -d | gzip -dc > "$staging/`+name+`"`)
	}
	lines = append(lines,
		`cd "$staging"`,
		sums+" | sha256sum --check --status --strict",
		"chmod 0644 "+names,
	)
	for _, name := range a.names {
		// -T replaces whatever holds the name, even a directory, rather than moving the file into it.
		lines = append(lines, `mv -fT `+name+` "$adapter/`+name+`"`)
	}
	lines = append(lines, `cd "$adapter"`, `guarded 'regular file' `+names, sums+" | sha256sum --check --status --strict")
	return strings.Join(append(lines, ")", "fi"), "\n")
}

// onHost answers command preceded by the adapter's installation, all under `set -eu`, so a failed install
// fails the command before it runs on an adapter that does not match this launcher.
func (l *Launcher) onHost(command string) string {
	if l.Adapter == nil {
		return "set -eu\n" + command
	}
	return "set -eu\n" + l.Adapter.Script() + "\n" + command
}
