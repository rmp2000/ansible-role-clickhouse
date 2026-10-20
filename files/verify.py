"""Root-only checks; credentials never leave their protected configuration file."""
import base64
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path('/etc/clickhouse-platform')
STATE = Path('/srv/clickhouse/.verification.json')
CONFIG = json.loads((ROOT/'credentials.json').read_text())
CONTEXT = ssl.create_default_context(cafile=str(ROOT/'tls/server.crt'))

def query(sql, user='platform_admin', timeout=60, **parameters):
    key = {'platform_admin':'admin_password','platform_writer':'writer_password','platform_reader':'reader_password'}[user]
    params = {'wait_end_of_query': '1', **{'param_'+k:str(v) for k,v in parameters.items()}}
    req = urllib.request.Request('https://'+CONFIG['host']+':8443/?'+urllib.parse.urlencode(params),
        data=sql.encode(), headers={'X-ClickHouse-User':user,'X-ClickHouse-Key':CONFIG[key]})
    with urllib.request.urlopen(req, context=CONTEXT, timeout=timeout) as r:
        return r.read().decode()

def wait_ready():
    end=time.monotonic()+180
    while time.monotonic()<end:
        try:
            if query('SELECT 1',timeout=5).strip()=='1':return
        except Exception:
            time.sleep(2)
    raise RuntimeError('Authenticated server readiness timed out')

def insert(identifier):
    row={'event_id':identifier,'record_type':'measurement_summary','sensor':identifier,'count':2,'sum':30,'mean':15,'payload_sha256':'a'*64,'payload':'verification'}
    query('INSERT INTO analytics.events FORMAT JSONEachRow\n'+json.dumps(row),'platform_writer')

def check(identifier):
    return query('SELECT count(),any(payload) FROM analytics.events FINAL WHERE event_id={id:String}', 'platform_reader',id=identifier).strip()=='1\tverification'

def remove(identifier):
    query('ALTER TABLE analytics.events DELETE WHERE event_id={id:String} SETTINGS mutations_sync=2',id=identifier)

def schema():
    before=int(query("SELECT count() FROM system.tables WHERE database='analytics' AND name IN ('events','rejected','measurements','sensor_latest')").strip())
    statements=[
        'CREATE DATABASE IF NOT EXISTS analytics',
        "CREATE TABLE IF NOT EXISTS analytics.events (event_id String, record_type LowCardinality(String), sensor String DEFAULT '', count UInt64 DEFAULT 0, sum Float64 DEFAULT 0, mean Float64 DEFAULT 0, reason String DEFAULT '', payload_sha256 String, payload String) ENGINE=ReplacingMergeTree ORDER BY event_id",
        'CREATE TABLE IF NOT EXISTS analytics.rejected (event_id String, reason String, payload_sha256 String, payload_base64 String) ENGINE=ReplacingMergeTree ORDER BY (event_id,payload_sha256)',
        "CREATE VIEW IF NOT EXISTS analytics.measurements AS SELECT event_id,sensor,count AS sample_count,sum AS total,mean AS average FROM analytics.events FINAL WHERE record_type='measurement_summary'",
        "CREATE VIEW IF NOT EXISTS analytics.sensor_latest AS SELECT sensor,argMax(count,count) AS sample_count,argMax(sum,count) AS total,argMax(mean,count) AS average FROM analytics.events FINAL WHERE record_type='measurement_summary' GROUP BY sensor"
    ]
    for statement in statements:query(statement)
    print('SCHEMA_CREATED' if before<4 else 'SCHEMA_OK')

def main():
    mode=sys.argv[1]
    if mode=='ready':
        if query('SELECT 1',timeout=5).strip()!='1':raise RuntimeError('Wrong readiness response')
        print('CLICKHOUSE_READY');return
    if mode=='schema':schema();return
    if mode=='stage':
        if STATE.exists():raise RuntimeError('A persistence test is already pending')
        identifier='verify_'+uuid.uuid4().hex
        insert(identifier);STATE.write_text(json.dumps({'id':identifier}));STATE.chmod(0o600)
        print('CLICKHOUSE_DURABLE_ROW_STAGED');return
    if mode=='check':
        identifier=json.loads(STATE.read_text())['id']
        wait_ready()
        if not check(identifier):raise RuntimeError('Durable row changed after restart')
        remove(identifier);STATE.unlink();print('CLICKHOUSE_PERSISTENCE_OK');return
    if mode=='restart':
        subprocess.run([sys.executable,__file__,'stage'],check=True,capture_output=True)
        subprocess.run(['systemctl','restart','clickhouse-server'],check=True,capture_output=True)
        main_mode='check'
        subprocess.run([sys.executable,__file__,main_mode],check=True)
        return
    identifier='verify_'+uuid.uuid4().hex
    insert(identifier)
    try:
        if mode=='backup':
            archive='verification-'+uuid.uuid4().hex
            restored='restore_'+uuid.uuid4().hex
            query("BACKUP DATABASE analytics TO Disk('backups', '"+archive+"')")
            try:
                query("RESTORE DATABASE analytics AS "+restored+" FROM Disk('backups', '"+archive+"')")
                count=query('SELECT count() FROM '+restored+'.events FINAL WHERE event_id={id:String}',id=identifier).strip()
                if count!='1':raise RuntimeError('Restored backup does not contain the exact fixture')
            finally:
                query('DROP DATABASE IF EXISTS '+restored+' SYNC')
            print('CLICKHOUSE_BACKUP_RESTORE_OK');return
        if mode!='smoke':raise RuntimeError('Unknown check')
        settings=query("SELECT count() FROM system.merge_tree_settings WHERE name IN ('fsync_after_insert','fsync_part_directory') AND value='1'").strip()
        if settings!='2':raise RuntimeError('Durable insert and directory synchronization must both be enabled')
        insert(identifier)
        if not check(identifier):raise RuntimeError('Logical event uniqueness or exact payload check failed')
        try:
            query('CREATE TABLE analytics.forbidden(x Int32) ENGINE=Memory','platform_reader')
        except urllib.error.HTTPError:pass
        else:raise RuntimeError('Read-only account accepted a write')
        request=urllib.request.Request('https://'+CONFIG['host']+':8443/',data=b'SELECT 1',headers={'X-ClickHouse-User':'platform_reader','X-ClickHouse-Key':'invalid'})
        try:
            urllib.request.urlopen(request,context=CONTEXT,timeout=10)
        except urllib.error.HTTPError:pass
        else:raise RuntimeError('Invalid authentication was accepted')
        print('CLICKHOUSE_SMOKE_OK')
    finally:remove(identifier)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('CLICKHOUSE_CHECK_FAILED: '+(str(error) if type(error) is RuntimeError else type(error).__name__),file=sys.stderr)
        sys.exit(1)
