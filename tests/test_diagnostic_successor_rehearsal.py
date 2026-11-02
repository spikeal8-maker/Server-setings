"""Synthetic harness boundaries; never reaches an installation or network."""
import contextlib
import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('rehearsal',Path(__file__).parents[1]/'scripts/diagnostic_successor_rehearsal.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)
SECRET=b'SYNTHETIC-KEY-PAYLOAD-NEVER-OUTPUT'

class Contracts(unittest.TestCase):
    def test_default_plan_closed_errors_and_no_private_output(self):
        args=['--app-source','/source','--guard-tool','/guard','--snapshot','/copy','--destination','/fresh']
        with patch.object(m,'rehearse',return_value={'status':'PLAN'}) as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(args),0);self.assertFalse(run.call_args.args[-1])
        with patch.object(m,'rehearse',side_effect=RuntimeError(str(SECRET))),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main(args+['--apply']),1)
        self.assertNotIn('SYNTHETIC',output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'],'BLOCKED')

    def test_isolation_rejected_before_any_argument_io(self):
        result=subprocess.run([sys.executable,'-B',str(Path(m.__file__)), '--apply'],capture_output=True,text=True,check=False)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['code'],'ISOLATED_PYTHON_REQUIRED')

    def test_closed_eight_file_identity_and_incident(self):
        self.assertEqual(len(m.PINS),8)
        self.assertEqual(len(m.APP_REVISION),40);self.assertEqual(len(m.MANIFEST_SHA),64)
        for path,digest in m.PINS.items():
            self.assertEqual(len(digest),64);m.relative(path)
        self.assertEqual({p for _,p in m.MODULES},set(m.PINS)-{'apps/api/src/database-diagnostic-contract.json'})
        self.assertNotEqual(m.sha(b'arbitrary source'),m.PINS['tools/collect_logs.py'])

    def test_unsafe_paths_closed(self):
        for path in ('/absolute','../file','a/../b','a//b','a/./b','a\\b',''):
            with self.subTest(path=path),self.assertRaises(m.Blocked):m.relative(path)

    def test_all_source_bytes_verified_before_any_code_execution(self):
        fake_guard=b'raise AssertionError("untrusted code executed")'
        payloads={name:b'bad source' for name in m.PINS}
        def read(root,name,limit):return fake_guard if name=='guard.py' else payloads[name]
        with patch.object(m,'directory',return_value=123),patch.object(m.os,'close'), \
             patch.object(m,'read',side_effect=read),patch.object(m,'GUARD_SHA',m.sha(fake_guard)):
            with self.assertRaisesRegex(m.Blocked,'RUNTIME_IDENTITY_MISMATCH'):
                with m.trusted_runtime('/source','/guard.py'):self.fail('must not yield')

    def test_original_snapshot_revalidation_runs_even_after_rehearsal_failure(self):
        manifest={'copies':{'raw/data':{'bytes':1}}};body=json.dumps(manifest).encode();calls=[]
        class Reader:
            fd=123
            def __init__(self,*args,**kwargs):pass
            def __call__(self,name,limit):return body if name=='manifest.private.json' else b'x'
            def revalidate(self):calls.append('revalidate')
            def close(self):calls.append('close')
        def plan(args):print('{"status":"PLAN"}');return 0
        helper=types.SimpleNamespace(main=plan,Reader=Reader)
        guard=types.SimpleNamespace(__file__='/guard',inventory=lambda fd:{'raw/data':{},'manifest.private.json':{},'receipt.json':{}})
        with self.assertRaisesRegex(RuntimeError,'synthetic failure'):
            with m.verified_snapshot('/snapshot',m.sha(body),helper,guard):raise RuntimeError('synthetic failure')
        self.assertEqual(calls,['revalidate','close'])
        guard.inventory=lambda fd:{'raw/data':{},'manifest.private.json':{},'receipt.json':{},'unexpected':{}}
        with self.assertRaisesRegex(m.Blocked,'FORENSIC_TREE_MISMATCH'):
            with m.verified_snapshot('/snapshot',m.sha(body),helper,guard):self.fail('unexpected file accepted')


class DatabaseBoundaries(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE events(id,payload);CREATE TABLE state(key PRIMARY KEY,value);CREATE TABLE segment_verification(name,proof);")
        self.db.execute('INSERT INTO events VALUES(?,?)',(b'identity',SECRET))
        self.db.execute('INSERT INTO state VALUES(?,?)',('source:error','ERROR'))
        self.db.execute('INSERT INTO segment_verification VALUES(?,?)',('current','authentic'))
        self.db.commit()

    def test_fingerprint_all_rows_schema_and_only_two_state_exclusions(self):
        baseline=m.table_fingerprints(self.db,True)
        for key in m.ALLOWED_STATE:self.db.execute('INSERT INTO state VALUES(?,?)',(key,'new metadata'))
        self.assertEqual(m.table_fingerprints(self.db,True),baseline)
        complete=m.table_fingerprints(self.db)
        self.db.execute("UPDATE state SET value='OK' WHERE key='source:error'")
        self.assertNotEqual(m.table_fingerprints(self.db,True),baseline)
        self.assertNotEqual(m.table_fingerprints(self.db),complete)
        self.db.rollback()
        self.db.execute("UPDATE events SET payload='lost'")
        self.assertNotEqual(m.table_fingerprints(self.db,True),baseline)
        self.db.rollback();self.db.execute('CREATE TABLE extra(value)')
        self.assertNotEqual(m.table_fingerprints(self.db,True),baseline)

    def test_negative_faults_are_required_and_leave_every_table_unchanged(self):
        data=gzip.compress(b'{"id":"synthetic","message":"original"}\n',mtime=0)
        class Reader:
            def __call__(self,name,limit):return data
            def revalidate(self):pass
        reader=Reader();expected={'proposals':{'a.gz':{},'b.gz':{}}}
        class Core:
            RecoveryBlocked=m.Blocked
            @staticmethod
            def apply_metadata(db,probe,plan,manifest):
                probe.before_commit();db.execute('BEGIN IMMEDIATE')
                try:
                    m.need(set(plan['proposals'])=={'a.gz','b.gz'},'LEGACY_DEBT_CAS_CHANGED')
                    m.need(probe('diagnostics/store/a.gz',4096)==data,'EXACT_EVENT_BODY_MISMATCH')
                    db.execute('INSERT INTO state VALUES(?,?)',(m.ALLOWED_STATE[0],'metadata'))
                    db.execute('INSERT INTO state VALUES(?,?)',(m.ALLOWED_STATE[1],'ledger'))
                    probe.before_commit();db.commit()
                except BaseException:db.rollback();raise
        baseline=m.table_fingerprints(self.db)
        m.negative_challenges(self.db,reader,expected,Core,'f'*64)
        self.assertEqual(m.table_fingerprints(self.db),baseline)
        class Broken(Core):
            @staticmethod
            def apply_metadata(db,probe,plan,manifest):
                db.execute("UPDATE events SET payload='corrupt'");db.commit();raise m.Blocked('bad core')
        with self.assertRaisesRegex(m.Blocked,'NEGATIVE_MUTATED_INDEX'):
            m.negative_challenges(self.db,reader,expected,Broken,'f'*64)

    def test_challenge_cannot_pass_without_actual_expected_failure(self):
        class Reader:
            def revalidate(self):pass
        core=types.SimpleNamespace(RecoveryBlocked=m.Blocked,apply_metadata=lambda *args:None)
        with self.assertRaisesRegex(m.Blocked,'NEGATIVE_DID_NOT_FAIL'):
            m.negative_challenges(self.db,Reader(),{'proposals':{'a':{}}},core,'f'*64)

    def test_cold_verifier_uses_fresh_memory_db_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            asa=Path(temp);private=asa/'diagnostics/private';private.mkdir(parents=True)
            (private/'segment-verification.key').write_bytes(SECRET)
            store=asa/'diagnostics/store';store.mkdir();(store/'catalog.json').write_text('{"segments":[{"file":"active"}]}',encoding='utf-8')
            index=private/'index.sqlite'
            db=sqlite3.connect(index)
            db.executescript('CREATE TABLE events(id,published);CREATE TABLE state(key,value);')
            db.executemany('INSERT INTO events VALUES(?,?)',[(i,1) for i in range(7)])
            db.commit();db.close();before=index.read_bytes()
            class Collector:
                def __init__(self,*args):raise AssertionError('constructor forbidden')
                def validate_history_index(self):
                    self.verified_metadata={'active':{}}
                    assert self.db.execute('SELECT count(*) FROM events').fetchone()[0]==7
                    self.db.execute("INSERT INTO state VALUES('audit','in-memory only')")
                def verify_affected_segment(self,meta,mapping=True):assert mapping is False
            helper=types.SimpleNamespace(state=lambda db,key: {'retired':{}} if key in ('retired:segments','retired:metadata') else {})
            collector=types.SimpleNamespace(Collector=Collector,verify_successor_retirements=lambda *args:None)
            result=m.cold_verify(index,asa,helper,collector,{'proposals':{'retired':{}}})
            self.assertEqual(result['indexedRows'],7);self.assertEqual(result['activeSegmentsVerified'],1)
            self.assertEqual(result['retiredSegmentsVerified'],1);self.assertEqual(index.read_bytes(),before)
            db=sqlite3.connect(index)
            self.assertEqual(db.execute('SELECT count(*) FROM state').fetchone()[0],0);db.close()

@unittest.skipUnless(os.name=='posix','POSIX dirfd/no-follow semantics')
class FilesystemBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700)
        self.fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY);self.addCleanup(os.close,self.fd)
        self.owner=patch.object(m,'OWNER_UID',os.getuid());self.owner.start();self.addCleanup(self.owner.stop)

    def test_private_copy_is_exclusive_no_hardlinks_and_no_source_change(self):
        source=self.root/'source';source.write_bytes(SECRET);source.chmod(0o600)
        original=source.read_bytes();m.private_write(self.fd,'clone/private/data',m.read(self.fd,'source',1024,True))
        clone=self.root/'clone/private/data'
        self.assertEqual(clone.read_bytes(),original)
        self.assertNotEqual(clone.stat().st_ino,source.stat().st_ino)
        self.assertEqual(clone.stat().st_nlink,1);self.assertEqual(clone.stat().st_mode&0o777,0o600)
        self.assertEqual(clone.parent.stat().st_mode&0o777,0o700)
        with self.assertRaises(FileExistsError):m.private_write(self.fd,'clone/private/data',b'overwrite')
        self.assertEqual(source.read_bytes(),original);self.assertEqual(clone.read_bytes(),original)

    def test_symlink_hardlink_and_non_private_files_are_rejected(self):
        source=self.root/'source';source.write_bytes(SECRET);source.chmod(0o600)
        alias=self.root/'alias';alias.symlink_to(source)
        with self.assertRaises(OSError):m.read(self.fd,'alias',1024,True)
        alias.unlink();os.link(source,alias)
        with self.assertRaisesRegex(m.Blocked,'UNSAFE_FILE'):m.read(self.fd,'alias',1024,True)
        alias.unlink();source.chmod(0o640)
        with self.assertRaisesRegex(m.Blocked,'PRIVATE_FILE_REQUIRED'):m.read(self.fd,'source',1024,True)
        self.assertEqual(source.read_bytes(),SECRET)

    def test_host_clone_uses_standalone_backup_not_original_index_or_sidecars(self):
        payloads={'raw/'+m.INDEX_REL:b'raw index', 'raw/'+m.INDEX_REL+'-wal':b'raw wal',
                  'raw/diagnostics/private/segment-verification.key':SECRET,
                  'raw/diagnostics/store/catalog.json':b'catalog', 'sqlite-backup/test.sqlite':b'consistent standalone backup'}
        manifest={'copies':{n:{'bytes':len(v)} for n,v in payloads.items()}}
        manifest['copies']['raw/diagnostics/private/empty/']={}
        before=copy.deepcopy(payloads)
        m.copy_host(lambda name,limit:payloads[name],manifest,self.fd,'sqlite-backup/test.sqlite')
        self.assertEqual((self.root/'.asa'/m.INDEX_REL).read_bytes(),b'consistent standalone backup')
        self.assertFalse((self.root/'.asa'/(m.INDEX_REL+'-wal')).exists())
        self.assertTrue((self.root/'.asa/diagnostics/private/empty').is_dir())
        self.assertEqual(payloads,before)
        m.sync_tree(self.fd)

    def test_partial_copy_is_preserved_private_never_overwritten_or_deleted(self):
        manifest={'copies':{'raw/first':{'bytes':3},'raw/second':{'bytes':3}}}
        def failing(name,limit):
            if name=='raw/second':raise OSError('synthetic copy fault')
            return b'one'
        with self.assertRaises(OSError):m.copy_host(failing,manifest,self.fd,'backup')
        self.assertEqual((self.root/'.asa/first').read_bytes(),b'one')
        self.assertEqual((self.root/'.asa/first').stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/'.asa/second').exists())
        with self.assertRaises(FileExistsError):m.copy_host(failing,manifest,self.fd,'backup')
        self.assertEqual((self.root/'.asa/first').read_bytes(),b'one')

    def test_copy_parent_link_and_existing_directory_not_followed(self):
        outside=self.root/'outside';outside.mkdir();(self.root/'linked').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(OSError):m.private_write(self.fd,'linked/file',SECRET)
        self.assertFalse((outside/'file').exists())
        m.private_mkdir(self.fd,'empty/private')
        self.assertEqual((self.root/'empty/private').stat().st_mode&0o777,0o700)

if __name__=='__main__':unittest.main()
