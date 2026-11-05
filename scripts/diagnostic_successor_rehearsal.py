#!/usr/bin/env python3
"""Incident-only offline working-clone rehearsal. PLAN unless --apply; no live services."""
import sys
if __name__ == '__main__' and (not sys.flags.isolated or not sys.dont_write_bytecode):
    print('{"status":"BLOCKED","code":"ISOLATED_PYTHON_REQUIRED"}')
    raise SystemExit(1)
import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import types

APP_REVISION = '6a98bddd8953eff732ec7d2cbb3de8cdec8abdee'
REHEARSAL_ROOT = Path('/var/backups/asa-rehearsals')
MANIFEST_SHA = 'eccb0bf480fd636f1d527da4a4b78e024668d2f4e34dbc8e6f600d25481889d5'
GUARD_SHA = '5dd13624af168db3a3d1eb14eb0e9762506eb626dcccd102fe858dd266542e1e'
PINS = {
 'tools/retired_successor_recovery.py':'5404ba75b49ab3c24067eff10d62aa429860c9c2d731bb9590ffc078921eb7e3',
 'tools/collect_logs.py':'8e728afecab47a89ac73261fc588faf7be172f6ff32b77db16138d7d079a00f2',
 'tools/deployment/__init__.py':'b894d56afdcda00c7f34bab54c979d957581cadb120a414f207397850fd70a1b',
 'tools/deployment/paths.py':'517981b0847c1fb271eb407d2084ef4db08cc87f0a03bd0a8c84d8a73f6a434b',
 'tools/deployment/operational.py':'c538817ec94cc16df51cec1cc4054789a4f0c873595eaf6263987a2ca2909ffa',
 'tools/resource_sampling.py':'aa49ae8359a1036c034667ba216d105744f05cff9ee8d105b34c0bc1852337b5',
 'tools/memory_sampling.py':'55a5f5e68161973d9c37ff9b2ddcd5baaf1288152221beeaad4c33331d9995be',
 'apps/api/src/database-diagnostic-contract.json':'6eb0c5bb0a9af296bad53d3339c5d00e69392294e86abba95ed4c290ae3c435a',
}
MODULES = [('deployment','tools/deployment/__init__.py'), ('deployment.paths','tools/deployment/paths.py'),
 ('deployment.operational','tools/deployment/operational.py'), ('resource_sampling','tools/resource_sampling.py'),
 ('memory_sampling','tools/memory_sampling.py'), ('collect_logs','tools/collect_logs.py'),
 ('retired_successor_recovery','tools/retired_successor_recovery.py')]
INDEX_REL = 'diagnostics/private/index.sqlite'
ALLOWED_STATE = ('retired:metadata','retired:successor-recovery:v1')
LIMIT = 8*1024**3
OWNER_UID = 0

class Blocked(RuntimeError): pass

def need(value, code):
    if not value: raise Blocked(code)

def sha(data): return hashlib.sha256(data).hexdigest()
def normalized(data): return data.replace(b'\r\n',b'\n')
def encode(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

def relative(name):
    path=PurePosixPath(name)
    need(isinstance(name,str) and name and not path.is_absolute() and
         all(p not in ('','..','.') for p in name.rstrip('/').split('/')) and '\\' not in name,
         'UNSAFE_RELATIVE_PATH')
    return path

def signature(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_uid,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def directory(path, private=False):
    path=Path(path); need(path.is_absolute() and '..' not in path.parts,'ABSOLUTE_PATH_REQUIRED')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
            info=os.fstat(fd)
            need(info.st_uid==OWNER_UID and not info.st_mode&0o022,'UNTRUSTED_PARENT')
        if private: need(stat.S_IMODE(os.fstat(fd).st_mode)==0o700,'PRIVATE_DIRECTORY_REQUIRED')
        return fd
    except BaseException: os.close(fd);raise

@contextlib.contextmanager
def opened(root,name):
    parts=relative(name).parts;parent=os.dup(root)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            info=os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==OWNER_UID and not info.st_mode&0o022,'UNSAFE_FILE')
            yield fd
        finally:os.close(fd)
    finally:os.close(parent)

def read(root,name,limit,private=False):
    with opened(root,name) as fd:
        before=os.fstat(fd)
        if private: need(stat.S_IMODE(before.st_mode)==0o600,'PRIVATE_FILE_REQUIRED')
        need(before.st_size<=limit,'FILE_LIMIT')
        data=bytearray()
        while len(data)<=limit:
            chunk=os.read(fd,min(1024**2,limit+1-len(data)))
            if not chunk:break
            data.extend(chunk)
        need(len(data)<=limit and signature(before)==signature(os.fstat(fd)),'FILE_RACE')
        return bytes(data)

def private_mkdir(root,name):
    parent=os.dup(root)
    try:
        for part in relative(name).parts:
            try:os.mkdir(part,0o700,dir_fd=parent)
            except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            need(os.fstat(child).st_uid==OWNER_UID and stat.S_IMODE(os.fstat(child).st_mode)==0o700,'PRIVATE_DIRECTORY_REQUIRED')
            os.close(parent);parent=child
        os.fsync(parent)
    finally:os.close(parent)

@contextlib.contextmanager
def writable_index(private,helper):
    with opened(private,'index.sqlite') as fd, contextlib.closing(helper.SQLiteSidecars(private,OWNER_UID)) as sidecars:
        info=os.fstat(fd)
        need(stat.S_IMODE(info.st_mode)==0o600,'PRIVATE_FILE_REQUIRED')
        def check():
            sidecars.check()
            current=os.stat('index.sqlite',dir_fd=private,follow_symlinks=False)
            need((current.st_dev,current.st_ino)==(info.st_dev,info.st_ino) and
                 stat.S_ISREG(current.st_mode) and current.st_nlink==1 and current.st_uid==OWNER_UID and
                 stat.S_IMODE(current.st_mode)==0o600,'INDEX_REPLACED')
        check()
        with contextlib.closing(sqlite3.connect('file:/proc/self/fd/'+str(private)+'/index.sqlite?mode=rw',uri=True)) as db:
            check();yield db,check


def private_write(root,name,data):
    parts=relative(name).parts;parent=os.dup(root)
    try:
        for part in parts[:-1]:
            try:os.mkdir(part,0o700,dir_fd=parent)
            except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            need(os.fstat(child).st_uid==OWNER_UID and stat.S_IMODE(os.fstat(child).st_mode)==0o700,'PRIVATE_DIRECTORY_REQUIRED')
            os.close(parent);parent=child
        fd=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fsync(out.fileno())
        os.fsync(parent)
    finally:os.close(parent)

def sync_tree(root):
    for name in os.listdir(root):
        info=os.stat(name,dir_fd=root,follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
            try:sync_tree(child)
            finally:os.close(child)
        else:
            with opened(root,name) as fd:os.fsync(fd)
    os.fsync(root)


@contextlib.contextmanager
def trusted_runtime(source,guard_path):
    """Validate ALL bytes before compiling any; never import installed application."""
    root=directory(source)
    guard_root=directory(Path(guard_path).parent)
    try:
        guard_bytes=read(guard_root,Path(guard_path).name,128*1024)
        need(sha(normalized(guard_bytes))==GUARD_SHA,'GUARD_IDENTITY_MISMATCH')
        data={name:read(root,name,2*1024**2) for name in PINS}
        need(all(sha(normalized(data[name]))==digest for name,digest in PINS.items()),'RUNTIME_IDENTITY_MISMATCH')
        need(not any(name in sys.modules for name,_ in MODULES),'RUNTIME_MODULE_COLLISION')
        guard=types.ModuleType('_forensic_guard');guard.__file__=str(guard_path)
        exec(compile(normalized(guard_bytes),str(guard_path),'exec'),guard.__dict__)
        loaded=[]
        try:
            for name,path in MODULES:
                module=types.ModuleType(name);module.__file__=str(Path(source)/path)
                module.__package__=name if name=='deployment' else name.rpartition('.')[0]
                if name=='deployment':module.__path__=[]
                sys.modules[name]=module;loaded.append(name)
                exec(compile(normalized(data[path]),module.__file__,'exec'),module.__dict__)
                if '.' in name:setattr(sys.modules[name.rpartition('.')[0]],name.rpartition('.')[2],module)
            need(all(read(root,name,2*1024**2)==body for name,body in data.items()),'RUNTIME_SOURCE_RACE')
            try:
                yield sys.modules['retired_successor_recovery'],sys.modules['collect_logs'],guard
            finally:
                need(all(read(root,name,2*1024**2)==body for name,body in data.items()),'RUNTIME_SOURCE_RACE')
        finally:
            for name in reversed(loaded):sys.modules.pop(name,None)
    finally:os.close(root);os.close(guard_root)

@contextlib.contextmanager
def verified_snapshot(path,manifest_sha,helper,guard):
    """Use the approved helper PLAN before any destination write; enumerate exact tree."""
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result=helper.main(['--snapshot',str(path),'--manifest-sha256',manifest_sha,'--guard-tool',guard.__file__])
    need(result==0 and json.loads(output.getvalue())['status']=='PLAN','SOURCE_PLAN_FAILED')
    reader=helper.Reader(path,guard,private=True)
    try:
        manifest=json.loads(reader('manifest.private.json',16*1024**2))
        need(sha(reader('manifest.private.json',16*1024**2))==manifest_sha,'MANIFEST_CHANGED')
        reader.manifest=manifest
        expected=set(manifest['copies'])|{'manifest.private.json','receipt.json'}
        actual=set(guard.inventory(reader.fd))
        need(actual==expected,'FORENSIC_TREE_MISMATCH')
        for name,meta in manifest['copies'].items():
            relative(name)
            if not name.endswith('/'):reader(name,max(1,meta['bytes']))
        try:
            yield reader,manifest
        finally:
            reader.revalidate()
            need(set(guard.inventory(reader.fd))==expected,'FORENSIC_TREE_CHANGED')
    finally:reader.close()

def table_fingerprints(db,allow_metadata=False):
    def scalar(value):
        return ['blob',bytes(value).hex()] if isinstance(value,bytes) else [type(value).__name__,value]
    result={}
    rows=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    result['schema']=sha(encode(rows))
    names=[r[1] for r in rows if r[0]=='table']
    need(len(names)<=100,'TABLE_LIMIT')
    for name in names:
        quoted='"'+name.replace('"','""')+'"'
        sql='SELECT * FROM '+quoted
        if allow_metadata and name=='state':sql+=' WHERE key NOT IN (?,?)';args=ALLOWED_STATE
        else:args=()
        digests=sorted(sha(encode([scalar(v) for v in row])) for row in db.execute(sql,args))
        result[name]=(len(digests),sha(encode(digests)))
    return result

class CloneReader:
    def __init__(self,root,helper,guard):self.reader=helper.Reader(root,guard,private=True)
    def __call__(self,name,limit):return self.reader(name,limit)
    def revalidate(self):self.reader.revalidate()
    def close(self):self.reader.close()

def negative_challenges(db,reader,expected,helper,manifest_sha):
    baseline=table_fingerprints(db)
    class Challenge:
        def __init__(self,kind):self.kind=kind;self.calls=0
        def __call__(self,name,limit):
            data=reader(name,limit)
            if self.kind=='tamper' and name=='diagnostics/store/'+next(iter(expected['proposals'])):
                raw=gzip.decompress(data) if name.endswith('.gz') else data
                lines=raw.splitlines();event=json.loads(lines[0]);event['message']=str(event.get('message',''))+' [offline negative challenge]'
                lines[0]=encode(event);changed=b'\n'.join(lines)+b'\n'
                return gzip.compress(changed,mtime=0) if name.endswith('.gz') else changed
            return data
        def revalidate(self):reader.revalidate()
        def before_commit(self):
            self.calls+=1
            if self.kind=='precommit' and self.calls==2:raise Blocked('INJECTED_PRECOMMIT')
    for kind in ('tamper','partial','precommit'):
        probe=Challenge(kind);plan=expected
        if kind=='partial':
            plan=dict(expected,proposals=dict(expected['proposals']));plan['proposals'].pop(next(iter(plan['proposals'])))
        try:helper.apply_metadata(db,probe,plan,manifest_sha)
        except (helper.RecoveryBlocked,Blocked,ValueError,OSError,EOFError) as exc:reason=str(exc)
        else:raise Blocked('NEGATIVE_DID_NOT_FAIL')
        need(not db.in_transaction and table_fingerprints(db)==baseline,'NEGATIVE_MUTATED_INDEX')
        allowed={'tamper':{'EXACT_EVENT_BODY_MISMATCH','SUCCESSOR_CAS_CHANGED'},
                 'partial':{'LEGACY_DEBT_CAS_CHANGED'},'precommit':{'INJECTED_PRECOMMIT'}}
        need(reason in allowed[kind],'NEGATIVE_WRONG_REASON')
        if kind=='precommit':need(probe.calls==2,'PRECOMMIT_NOT_REACHED')
        reader.revalidate()


def copy_host(frozen,manifest,clone,backup_relative):
    for name,meta in manifest['copies'].items():
        if not name.startswith('raw/'):continue
        if name.endswith('/'):
            private_mkdir(clone,'.asa/'+name[4:].rstrip('/'));continue
        relative_name=name[4:]
        if relative_name in tuple(INDEX_REL+s for s in ('','-wal','-shm','-journal')):continue
        private_write(clone,'.asa/'+relative_name,frozen(name,max(meta['bytes'],1)))
    private_write(clone,'.asa/'+INDEX_REL,frozen(backup_relative,256*1024**2))


def cold_verify(index,asa,helper,collector,expected):
    # A fresh connection, not prior verification cache. Normal methods may write
    # audit/receipts, so use an in-memory SQLite backup and discard all such writes.
    with contextlib.closing(sqlite3.connect('file:'+str(index)+'?mode=ro&immutable=1',uri=True)) as cold:
        memory=sqlite3.connect(':memory:');cold.backup(memory)
    try:
        obj=collector.Collector.__new__(collector.Collector)
        obj.db=memory;obj.store=asa/'diagnostics/store';obj.private=asa/'diagnostics/private'
        obj.verification_key=(obj.private/'segment-verification.key').read_bytes()
        obj.validate_history_index()
        catalog=json.loads((obj.store/'catalog.json').read_text(encoding='utf-8'))
        need(len(obj.verified_metadata)==len(catalog['segments']),'WHOLE_ACTIVE_HISTORY_INCOMPLETE')
        retired=helper.state(memory,'retired:segments');metadata=helper.state(memory,'retired:metadata')
        collector.verify_successor_retirements(retired,metadata,helper.state(memory,'retired:successor-recovery:v1'),obj.verification_key)
        need(set(metadata)==set(retired),'RETIRED_METADATA_INCOMPLETE')
        for name in retired:obj.verify_affected_segment(metadata[name],mapping=False)
        need(set(expected['proposals'])<=set(retired),'RECOVERED_TARGET_LOST')
        counts=memory.execute('SELECT count(*),sum(published=1),sum(published=0) FROM events').fetchone()
        return dict(indexedRows=counts[0],publishedRows=counts[1] or 0,unpublishedRows=counts[2] or 0,
                    activeSegmentsVerified=len(obj.verified_metadata),retiredSegmentsVerified=len(retired))
    finally:memory.rollback();memory.close()


def rehearse(source,snapshot,destination,guard_path,apply=False):
    need(os.name=='posix' and os.geteuid()==0,'ROOT_LINUX_REQUIRED')
    snapshot=Path(snapshot);destination=Path(destination)
    need(destination.parent==REHEARSAL_ROOT,'REHEARSAL_PARENT_REQUIRED')
    need(snapshot.is_absolute() and destination.is_absolute() and '..' not in snapshot.parts and '..' not in destination.parts and
         not destination.is_relative_to(snapshot) and not snapshot.is_relative_to(destination),'OVERLAPPING_PATHS')
    for protected in (Path(source),Path(guard_path).parent):
        need(not destination.is_relative_to(protected) and not protected.is_relative_to(destination),'OVERLAPPING_PATHS')
    parent=directory(destination.parent,private=True)
    clone=None;previous_umask=None
    try:
        need(destination.name not in os.listdir(parent),'DESTINATION_EXISTS')
        with trusted_runtime(source,guard_path) as (helper,collector,guard):
            with verified_snapshot(snapshot,MANIFEST_SHA,helper,guard) as (frozen,manifest):
                with guard.opened(frozen.fd,helper.INDEX) as fd:
                    with contextlib.closing(sqlite3.connect('file:/proc/self/fd/'+str(fd)+'?mode=ro&immutable=1',uri=True)) as db:
                        db.execute('PRAGMA query_only=ON')
                        expected=helper.analyze(db,lambda n,l:frozen('raw/'+n,l),MANIFEST_SHA)
                need((expected['files'],expected['records'],expected['successors'])==(31,607,1),'INCIDENT_TARGET_MISMATCH')
                summary=dict(status='PLAN',files=31,records=607,successors=1,appRevision=APP_REVISION,
                             forensicManifestSha256=MANIFEST_SHA,historyHealthy=False,liveApplied=False,
                             guardSha256=GUARD_SHA,runtimeClosureSha256=sha(encode(PINS)),
                             qualification='OFFLINE_METADATA_REHEARSAL_ONLY',fullCollectorStartup=False)
                if not apply:return summary
                size=sum(v.get('bytes',0) for v in manifest['copies'].values())
                space=os.fstatvfs(parent)
                need(space.f_bavail*space.f_frsize>=size*3+64*1024**2 and space.f_favail>=len(manifest['copies'])*3+64,'INSUFFICIENT_SPACE')
                previous_umask=os.umask(0o077)
                os.mkdir(destination.name,0o700,dir_fd=parent)
                clone=os.open(destination.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                # Keep every original byte in the source. Clone HOST layout uses
                # the standalone SQLite backup, never copied live WAL/journal.
                copy_host(frozen,manifest,clone,helper.INDEX)
                asa=destination/'.asa';reader=CloneReader(asa,helper,guard)
                try:
                    before_files=guard.inventory(clone)
                    private=directory(asa/'diagnostics/private',private=True)
                    try:
                        with writable_index(private,helper) as (db,check):
                                reader.before_commit=check
                                before=table_fingerprints(db,True)
                                old_metadata=helper.state(db,ALLOWED_STATE[0])
                                old_ledger=helper.state(db,ALLOWED_STATE[1])
                                negative_challenges(db,reader,expected,helper,MANIFEST_SHA)
                                helper.apply_metadata(db,reader,expected,MANIFEST_SHA)
                                need(table_fingerprints(db,True)==before,'NON_METADATA_CHANGED')
                                need(helper.state(db,ALLOWED_STATE[0])=={**old_metadata,**expected['proposals']},'METADATA_POSTCONDITION_FAILED')
                                new_ledger=helper.state(db,ALLOWED_STATE[1])
                                need(set(new_ledger)==set(old_ledger)|{expected['fingerprint']} and
                                     all(new_ledger[k]==v for k,v in old_ledger.items()),'LEDGER_POSTCONDITION_FAILED')
                                replay=helper.analyze(db,reader,MANIFEST_SHA,expected['proposals'])
                                need(replay['fingerprint']==expected['fingerprint'],'RECOVERED_BODY_CHANGED')
                    finally:os.close(private)
                    reader.revalidate()
                    # Physical SQLite layout may change; all other copied bytes
                    # must remain exact, and every DB table/schema is checked above.
                    after_files=guard.inventory(clone)
                    def payloads(items):return {k:(v.get('bytes'),v.get('sha256')) for k,v in items.items() if k!='.asa/'+INDEX_REL and not k.endswith('/')}
                    need(payloads(before_files)==payloads(after_files),'CLONE_SOURCE_BYTES_CHANGED')
                    summary.update(cold_verify(asa/INDEX_REL,asa,helper,collector,expected))
                    need(payloads(after_files)==payloads(guard.inventory(clone)),'COLD_VERIFY_CHANGED_FILES')
                    summary.update(status='OFFLINE_REHEARSAL_PASSED',negativeChallenges=3)
                    frozen.revalidate()
                finally:reader.close()
        sync_tree(clone)
        private_write(clone,'rehearsal.private.json',encode(summary))
        os.fsync(parent)
        return summary
    except BaseException:
        if clone is not None:
            private_write(clone,'FAILED.private.json',encode(dict(status='FAILED',liveApplied=False,historyHealthy=False)))
        raise
    finally:
        if clone is not None:os.close(clone)
        if previous_umask is not None:os.umask(previous_umask)
        os.close(parent)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-source',required=True);parser.add_argument('--guard-tool',required=True)
    parser.add_argument('--snapshot',required=True);parser.add_argument('--destination',required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args(argv)
    try:
        result=rehearse(args.app_source,args.snapshot,args.destination,args.guard_tool,args.apply)
    except BaseException:
        print(json.dumps(dict(status='BLOCKED',code='OFFLINE_REHEARSAL_FAILED',historyHealthy=False,liveApplied=False)))
        return 1
    print(json.dumps(result,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
