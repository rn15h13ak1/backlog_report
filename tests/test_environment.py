"""
依存が入っていない Python で起動したときの案内のテスト。

開発機には依存が入っているため、この経路が壊れても他のテストは全部通る。
`sitecustomize` に import を名前ごと撥ねる finder を差し込んで再現する
（アンインストールはしない）。

本ツールは `import yaml` を直接書いており、`importlib.util.find_spec()` で
存在を調べてはいないため、この細工で忠実に再現できる。
"""
import os
import subprocess
import sys
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[1] / "backlog_weekly_report.py"

_SITECUSTOMIZE = '''
import sys

BLOCKED = {blocked!r}


class _Blocker:
    def find_module(self, name, path=None):
        return None

    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED or name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
        return None


sys.meta_path.insert(0, _Blocker())
'''


def run_entry(tmp_path: Path, blocked=()) -> subprocess.CompletedProcess:
    """入口を別プロセスで起動する。blocked に挙げた import は失敗させる。"""
    env = dict(os.environ)
    if blocked:
        (tmp_path / "sitecustomize.py").write_text(
            _SITECUSTOMIZE.format(blocked=set(blocked)), encoding="utf-8")
        env["PYTHONPATH"] = str(tmp_path)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run([sys.executable, str(ENTRY), "--help"],
                          capture_output=True, text=True, env=env)


def test_missing_dependency_is_explained(tmp_path):
    """依存が無いときは、対処と実行中の Python を示して終了コード 2 で止まること"""
    result = run_entry(tmp_path, blocked=["yaml"])

    assert result.returncode == 2, result.stdout + result.stderr
    assert "必要なライブラリ yaml が入っていません" in result.stderr
    assert "pip install pyyaml" in result.stderr
    # どの Python で動いているかを出す（複数の Python が入っている環境向け）
    assert sys.executable in result.stderr
    assert "Traceback" not in result.stderr


def test_unrelated_import_error_is_not_swallowed(tmp_path):
    """
    案内の対象でない import の失敗は、そのまま送出すること。

    自前モジュールの綴り間違いまで「ライブラリを入れてください」と案内すると、
    本当の原因が隠れる。
    """
    result = run_entry(tmp_path, blocked=["backlog_report"])

    assert result.returncode == 1
    assert "Traceback" in result.stderr
    assert "必要なライブラリ" not in result.stderr


def test_entry_runs_normally_when_dependencies_are_present(tmp_path):
    """細工が空振りしていないこと（依存がある場合は通常どおり起動する）"""
    result = run_entry(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Backlog レポート生成" in result.stdout
