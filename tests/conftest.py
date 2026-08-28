import sys
from pathlib import Path

# tests/ から見てリポジトリルートを import パスに追加する
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
