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
project_key = config["projects"][0]["project_key"]

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
    qs = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{base_url}{endpoint}?{qs}"
    print(f"  → {endpoint}?{qs.replace(api_key, '***')}")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15, context=ctx) as res:
        return json.loads(res.read().decode("utf-8"))

print(f"接続先: {base_url}")
print(f"プロジェクト: {project_key}")
print()

# Step1: プロジェクト情報を取得して project_id と issue を1件取得
print("=== Step1: 課題を1件取得 ===")
issues = get(f"/projects/{project_key}/issues", {"count": 1})
if not issues:
    print("課題が1件も見つかりませんでした。")
    sys.exit(1)

issue = issues[0]
issue_id  = issue["id"]
issue_key = issue["issueKey"]
print(f"取得した課題: {issue_key} (id={issue_id})")
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
