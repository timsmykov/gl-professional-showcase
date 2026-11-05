"""Exercise migrations and API/DB contracts in one disposable local PostgreSQL container.

Requires Docker and the backend virtual environment. Only the uniquely named
test container is removed. No production configuration or credentials are read.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

root=Path(__file__).resolve().parents[1]/'backend'
name='portfolio-gl-test-'+uuid4().hex[:12]

def docker(*args):
 return subprocess.check_output(['docker',*args],text=True,stderr=subprocess.DEVNULL).strip()

async def provision(port):
 import asyncpg
 connection=await asyncpg.connect(host='127.0.0.1',port=port,user='postgres',database='postgres')
 try:
  await connection.execute("CREATE ROLE gl_api_app LOGIN PASSWORD 'synthetic-local-only'")
  await connection.execute("CREATE ROLE gl_api_migrator LOGIN CREATEDB PASSWORD 'synthetic-local-only'")
  await connection.execute('CREATE DATABASE gl_api OWNER gl_api_migrator')
 finally:await connection.close()

with tempfile.TemporaryDirectory(prefix='portfolio-gl-') as directory:
 secret=Path(directory)/'synthetic.txt';secret.write_text('synthetic-local-only');secret.chmod(0o600)
 created=False
 try:
  docker('run','--detach','--rm','--name',name,'--publish','127.0.0.1::5432','--env','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine')
  created=True
  for _ in range(60):
   ready=subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],capture_output=True).returncode==0
   if ready:break
   time.sleep(0.25)
  else:raise RuntimeError('Disposable PostgreSQL did not become ready')
  port=int(json.loads(docker('inspect',name))[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
  asyncio.run(provision(port))
  env=os.environ.copy()
  env.update(GL_API_DATABASE_HOST='127.0.0.1',GL_API_DATABASE_PORT=str(port),GL_API_DATABASE_NAME='gl_api',GL_API_DATABASE_USER='gl_api_app',GL_API_DATABASE_PASSWORD_FILE=str(secret),GL_API_MIGRATION_DATABASE_USER='gl_api_migrator',GL_API_MIGRATION_DATABASE_PASSWORD_FILE=str(secret),GL_API_TEST_DATABASE='1',GL_API_TEST_MIGRATOR_PASSWORD_FILE=str(secret),GL_API_TEST_ADMIN_PASSWORD_FILE=str(secret))
  for _ in range(2):
   result=subprocess.run([str(root/'.venv/bin/alembic'),'upgrade','head'],cwd=root,env=env,capture_output=True,text=True)
   if result.returncode:raise RuntimeError('Migration check failed; no sensitive diagnostics printed')
  print('Applied all migrations twice on a fresh synthetic database.',flush=True)
  result=subprocess.run([sys.executable,'-m','pytest','-q','tests/api/test_postgres.py','tests/api/test_orders_postgres.py','tests/api/test_lifecycle_postgres.py','tests/test_api_recordings_postgres.py'],cwd=root,env=env)
  if result.returncode:raise SystemExit(result.returncode)
 finally:
  if created:docker('rm','--force',name)
