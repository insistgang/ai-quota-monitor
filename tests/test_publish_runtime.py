import datetime as dt
import importlib.util
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_ENTRYPOINT = REPO_ROOT / "scripts" / "quota-publish-launchd.sh"
RUNTIME_INSTALLER = REPO_ROOT / "scripts" / "install-launchd-runtime.sh"
LAUNCHD_PLIST = REPO_ROOT / "examples" / "com.leo.quota-report.plist"
PUBLISH_RUNTIME = REPO_ROOT / "publish_runtime.py"


def _load_publish_runtime():
    spec = importlib.util.spec_from_file_location("publish_runtime", PUBLISH_RUNTIME)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 publish_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


class LaunchdEntrypointTests(unittest.TestCase):
    def test_plist_logs_are_written_under_a_tcc_safe_user_library_directory(self):
        self.assertTrue(LAUNCHD_PLIST.is_file(), "缺少 launchd 配置模板")
        config = plistlib.loads(LAUNCHD_PLIST.read_bytes())
        expected_log_dir = Path("/Users/YOURNAME/Library/Logs/ai-quota-monitor")

        for key in ("StandardOutPath", "StandardErrorPath"):
            log_path = Path(config[key])
            self.assertEqual(log_path.parent, expected_log_dir)
            self.assertNotIn("/Documents/", log_path.as_posix())

    def test_entrypoint_delegates_to_python_without_opening_publish_shell_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = Path(tmp)
            runtime = fake_repo / "publish_runtime.py"
            runtime.write_text("# runtime marker\n", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "QUOTA_MONITOR_HOME": str(fake_repo),
                "QUOTA_PYTHON_BIN": "/bin/echo",
            })

            result = subprocess.run(
                ["/bin/bash", str(LAUNCHD_ENTRYPOINT)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(runtime))

    def test_installer_copies_the_versioned_entrypoint_outside_documents(self):
        self.assertTrue(RUNTIME_INSTALLER.is_file(), "缺少 launchd 入口安装器")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_dir = root / "bin"
            env = os.environ.copy()
            env.update({
                "HOME": str(root / "home"),
                "QUOTA_RUNTIME_BIN_DIR": str(target_dir),
            })

            result = subprocess.run(
                ["/bin/bash", str(RUNTIME_INSTALLER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            installed = target_dir / "quota-publish"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(installed.read_bytes(), LAUNCHD_ENTRYPOINT.read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o755)

    def test_installer_creates_default_log_directory_under_isolated_home(self):
        self.assertTrue(RUNTIME_INSTALLER.is_file(), "缺少 launchd 入口安装器")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            target_dir = root / "bin"
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "QUOTA_RUNTIME_BIN_DIR": str(target_dir),
            })

            result = subprocess.run(
                ["/bin/bash", str(RUNTIME_INSTALLER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                (home / "Library" / "Logs" / "ai-quota-monitor").is_dir()
            )


class PublishRuntimeTests(unittest.TestCase):
    def test_runtime_generates_and_commits_only_the_three_public_pages(self):
        self.assertTrue(PUBLISH_RUNTIME.is_file(), "缺少集中式 Python 发布入口")
        publish_runtime = _load_publish_runtime()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "docs").mkdir()
            (repo / "quota_report.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path('report-args.txt').write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
                "for name in ('index.html', 'history.html', 'subscriptions.html'):\n"
                "    (Path('docs') / name).write_text(name, encoding='utf-8')\n",
                encoding="utf-8",
            )
            unrelated = repo / "notes.txt"
            unrelated.write_text("before\n", encoding="utf-8")
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.name", "Quota Test")
            _git(repo, "config", "user.email", "quota@example.invalid")
            _git(repo, "add", "--", "quota_report.py", "notes.txt")
            _git(repo, "commit", "-q", "-m", "initial")
            unrelated.write_text("after\n", encoding="utf-8")
            _git(repo, "add", "--", "notes.txt")

            committed = publish_runtime.publish(
                repo,
                push=False,
                now=dt.datetime(2026, 8, 21, 18, 30),
            )

            self.assertTrue(committed)
            changed = _git(
                repo, "show", "--pretty=format:", "--name-only", "HEAD",
            ).stdout.splitlines()
            self.assertEqual(changed, [
                "docs/history.html",
                "docs/index.html",
                "docs/subscriptions.html",
            ])
            self.assertEqual(
                _git(repo, "log", "-1", "--pretty=%s").stdout.strip(),
                "chore: 每日额度快照 2026-08-21_1830",
            )
            self.assertEqual(
                _git(repo, "diff", "--cached", "--name-only").stdout.strip(),
                "notes.txt",
            )
            self.assertEqual(
                (repo / "report-args.txt").read_text(encoding="utf-8").splitlines(),
                [
                    "--log",
                    "--html",
                    "--public-html",
                    "docs/index.html",
                    "--retry-failures",
                    "2",
                    "--retry-delay",
                    "15",
                    "--max-unhealthy",
                    "1",
                ],
            )

    def test_runtime_smoke_mode_is_read_only_and_returns_pass_marker(self):
        self.assertTrue(PUBLISH_RUNTIME.is_file(), "缺少集中式 Python 发布入口")
        before = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        env = os.environ.copy()
        env["QUOTA_PUBLISH_SMOKE_TEST"] = "1"

        result = subprocess.run(
            [sys.executable, str(PUBLISH_RUNTIME)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        after = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "INSTALL_RUNTIME_PASS")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
