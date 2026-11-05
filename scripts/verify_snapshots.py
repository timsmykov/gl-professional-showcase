"""Verify curated snapshots without private repositories or credentials."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
count=0
for manifest,folder in [('backend-snapshot.json','backend'),('site-snapshot.json','site')]:
 for row in json.loads((root/manifest).read_text())['files']:
  if hashlib.sha256((root/folder/row['path']).read_bytes()).hexdigest()!=row['sha256']:
   raise SystemExit(f'Snapshot mismatch: {folder}/{row["path"]}')
  count+=1
print(f'Verified {count} selected original source/asset files.')
