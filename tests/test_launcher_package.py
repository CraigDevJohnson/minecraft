import ast
from pathlib import Path
import re
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_idle_stop as package  # noqa: E402


class LauncherPackageTests(unittest.TestCase):
    """The launcher installs the adapter it carries (ADR 0007), so its zip must carry the adapter install_crafty.sh installs."""

    def test_the_adapter_lists_agree(self):
        installed = re.search(r'^install -m 0644 (.*) "\$install_dir/scripts/"$', (ROOT / "scripts" / "install_crafty.sh").read_text(), re.MULTILINE)
        self.assertIsNotNone(installed)
        self.assertEqual(set(re.findall(r'"\$source_dir/scripts/([a-z_]+\.py)"', installed.group(1))), set(package.ADAPTER_FILES))
        go = re.search(r"var adapterFiles = \[\]string\{([^}]*)\}", (ROOT / "lambda" / "launcher" / "hostadapter.go").read_text())
        self.assertIsNotNone(go)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', go.group(1))), package.ADAPTER_FILES)

    def test_the_adapter_carries_every_module_it_imports_from_scripts(self):
        for name in package.ADAPTER_FILES:
            tree = ast.parse((ROOT / "scripts" / name).read_text())
            for node in ast.walk(tree):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
                for module in modules:
                    if module and (ROOT / "scripts" / f"{module}.py").exists():
                        self.assertIn(f"{module}.py", package.ADAPTER_FILES, f"{name} imports {module}")

    def test_only_the_launcher_carries_the_adapter(self):
        self.assertEqual(package.bundled(package.FUNCTIONS["idle-stop"]), [])
        self.assertEqual(
            package.bundled(package.FUNCTIONS["launcher"]),
            [(f"adapter/{name}", ROOT / "scripts" / name) for name in package.ADAPTER_FILES],
        )

    def test_the_archive_is_reproducible_and_keeps_the_binary_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "bootstrap"
            binary.write_bytes(b"\x7fELF launcher")
            first, second = Path(directory) / "one.zip", Path(directory) / "two" / "launcher.zip"
            package.write_archive(first, binary, package.bundled(package.FUNCTIONS["launcher"]))
            package.write_archive(second, binary, package.bundled(package.FUNCTIONS["launcher"]))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertFalse(any(path.name.endswith(".partial") for path in Path(directory).rglob("*")))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["bootstrap", *(f"adapter/{name}" for name in package.ADAPTER_FILES)])
                for info in archive.infolist():
                    self.assertEqual(info.date_time, package.ZIP_TIME)
                    self.assertEqual(info.external_attr >> 16, 0o100755 if info.filename == "bootstrap" else 0o100644)
                self.assertEqual(archive.read("bootstrap"), binary.read_bytes())
                for name in package.ADAPTER_FILES:
                    self.assertEqual(archive.read(f"adapter/{name}"), (ROOT / "scripts" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
