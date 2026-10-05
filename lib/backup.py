"""個人資料版本備份：private/ 是獨立的本機 git repo（不推送到任何遠端），
記錄帳本、設定、情境與對帳檔的每一次變動。程式碼由專案根目錄的公開 repo 管理。"""
import subprocess
from datetime import datetime

from . import data

REPO = data.PRIVATE


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def snapshot(reason="auto"):
    if not (REPO / ".git").exists():
        _git("init", "-q")
    _git("add", "-A")
    if not _git("diff", "--cached", "--quiet").returncode:
        return None   # 沒有變動
    msg = f"snapshot: {reason} {datetime.now():%Y-%m-%d %H:%M}"
    r = _git("commit", "-q", "-m", msg)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return msg
