#!/usr/bin/env python3
"""
/issues/{id}/activities エンドポイントの動作確認スクリプト
config.yaml の設定を読み込んで実際にリクエストを送信します。
"""
import sys
import ssl
import json
import urllib.request
import urllib.parse
import yaml

# config.yaml を読み込む
with open("config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

bl = config["backlog"]
host      = bl["space_host"]
api_key   = bl["api_key"]
base_path = ("/" + bl.get("base_path", "").strip("/")) if bl.get("base_path", "").strip("/") else ""
ssl_verify = bl.get("ssl_verify", True)
project_key = bl["project_key"]

base_url = f"https://{host}{base_path}/api/v2"

# SSL設定
if ssl_verify:
    ctx = None
else:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

def get(endpoint, params=None):
    params = params or {}
    params["apiKey"] = api_key
    parts = []
    for k, v in params.items():
        if isinstance(v, list):
            for item in v:
                parts.append(f"{urllib.parse.quote(k)}%5B%5D={urllib.parse.quote(str(item))}")
        else:
            parts.append(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}")
    qs = "&".join(parts)
    url = f"{base_url}{endpoint}?{qs}"
    print(f"  → {endpoint}?{qs.replace(api_key, '***')}")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15, context=ctx) as res:
        return json.loads(res.read().decode("utf-8"))

print(f"接続先: {base_url}")
print(f"プロジェクト: {project_key}")
print()

# Step0: 接続・認証確認（/space は最もシンプルなエンドポイント）
print("=== Step0: 接続・認証確認 ===")
try:
    space = get("/space")
    print(f"✅ 接続OK: {space.get('name', space)}")
except Exception as e:
    print(f"❌ 接続失敗: {e}")
    print("  → space_host / base_path / api_key / ssl_verify を確認してください")
    sys.exit(1)
print()

# Step1: プロジェクト情報を取得して project_id と issue を1件取得
print("=== Step1: 課題を1件取得 ===")
print(f"  project_key の値: [{project_key}]")
try:
    project = get(f"/projects/{project_key}")
    print(f"✅ プロジェクト取得OK: {project.get('name')} (id={project.get('id')})")
except Exception as e:
    print(f"❌ プロジェクト取得失敗: {e}")
    print("  → project_key を確認してください")
    sys.exit(1)
project_id = project.get("id")
# ステータス変化を確認しやすいよう完了済み課題を優先して取得
issues = get("/issues", {"projectId": [project_id], "statusId": closed_status_ids, "count": 1})
if not issues:
    # 完了済みがなければ全ステータスで取得
    issues = get("/issues", {"projectId": [project_id], "count": 1})
if not issues:
    print("課題が1件も見つかりませんでした。")
    sys.exit(1)

issue = issues[0]
issue_id  = issue["id"]
issue_key = issue["issueKey"]
print(f"取得した課題: {issue_key} (id={issue_id}, status={issue.get('status', {}).get('name')})")
print()

# Step2: /issues/{id}/activities を試す
print("=== Step2: /issues/{id}/activities エンドポイント確認 ===")
try:
    activities = get(f"/issues/{issue_id}/activities", {"count": 1})
    print(f"✅ エンドポイント有効。取得件数: {len(activities)}")
    if activities:
        print(f"   サンプル: type={activities[0].get('type')}, created={activities[0].get('created', '')[:10]}")
except Exception as e:
    print(f"❌ エンドポイント無効またはエラー: {e}")
print()

# Step3: /issues/{id}/comments を試す（changeLog にステータス変化が含まれる可能性）
print("=== Step3: /issues/{id}/comments エンドポイント確認 ===")
try:
    comments = get(f"/issues/{issue_id}/comments", {"count": 20, "order": "desc"})
    print(f"✅ エンドポイント有効。取得件数: {len(comments)}")
    status_changes = []
    for c in comments:
        for cl in c.get("changeLog", []):
            if cl.get("field") == "status":
                status_changes.append({
                    "date": c.get("created", "")[:10],
                    "from": cl.get("originalValue"),
                    "to":   cl.get("newValue"),
                })
    if status_changes:
        print(f"   ステータス変化の記録あり（直近{len(status_changes)}件）:")
        for sc in status_changes:
            print(f"     {sc['date']}: {sc['from']} → {sc['to']}")
    else:
        print("   直近20コメント内にステータス変化なし")
        print("   ※ changeLog フィールドの有無を確認します")
        has_changelog = any("changeLog" in c for c in comments)
        print(f"   changeLog フィールド: {'あり' if has_changelog else 'なし（コメント構造が異なる可能性）'}")
        if comments:
            print(f"   コメント構造サンプル: {list(comments[0].keys())}")
except Exception as e:
    print(f"❌ エンドポイント無効またはエラー: {e}")
