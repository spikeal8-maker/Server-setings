"""Offline restore-contract tests; fixtures contain no production data or keys."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock
import zipfile

CODE = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(CODE))
import school_settings_snapshot as snapshot

spec = importlib.util.spec_from_file_location('config_roundtrip', CODE / 'school-settings-cloud-roundtrip.py')
roundtrip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roundtrip)
ARCHIVE = 'asa-school-config-01-20261108T091500Z-3f6a47b2.tar.age'


def normalized_zip(kind, *, coverage=False, extra=False):
    raw = (json.dumps({'id':'a'*64,'time':'2026-11-08T08:00:00.000Z','source':'api' if kind=='application' else 'systemd',
        'module':'host','level':'info','message':'Synthetic non-secret fixture'}, separators=(',',':'))+'\n').encode()
    segment = {'file':'records/part-00001.jsonl','schema':2,'bytes':len(raw),'rawBytes':len(raw),
        'count':1,'sha256':hashlib.sha256(raw).hexdigest()}
    manifest = {'normalized':True,'sourceStatusIncluded':False,'searchApplied':False,'count':1,
        'filter':{'scope':'all','from':'1970-01-01T00:00:00.000Z','to':'9999-12-31T23:59:59.999Z',
                  'source':'','module':'','level':'','before':None},
        'coverage':{'complete':coverage,'application':'embedded' if kind=='application' else 'host-collector'},
        'catalog':{'version':1,'sources':[],'segments':[segment],'bytes':len(raw)}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(segment['file'],raw)
        z.writestr('manifest.json',json.dumps(manifest))
        z.writestr('README.txt','Synthetic fixture only')
        if extra:
            z.writestr('../escape.txt','Never extract this')
    return buffer.getvalue()


def application_release(revision='a'*40, format='asa-deployment-v2'):
    return {'format':format,'repository':snapshot.REPOSITORY,'platform':'linux/amd64',
            'revision':revision,'schema':319,'publicationRunId':1234,
            'images':{name:'ghcr.io/'+snapshot.REPOSITORY+'/'+name+'@sha256:'+'b'*64
                      for name in snapshot.RELEASE_SERVICES[format]}}


def fixture(root):
    database = root/'fixture.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE settings(name TEXT PRIMARY KEY,value TEXT)')
        connection.execute('INSERT INTO settings VALUES (?,?)',('fixture','non-secret'))
    sqlite_bytes = database.read_bytes()
    payloads = {'files/'+str(p).lstrip('/'):('fixture:'+p.name).encode() for p in snapshot.STATIC}
    payloads.update({'sqlite/'+db:sqlite_bytes for db in snapshot.DB_NAMES})
    payloads.update({'normalized/'+kind+'.zip':normalized_zip(kind) for kind in ('host','application')})
    entries=[]
    for name,data in payloads.items():
        kind='config' if name.startswith('files/') else 'sqlite' if name.startswith('sqlite/') else 'normalized'
        entries.append({'member':name,'kind':kind,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
            'sourceMetadata':None if kind=='normalized' else {'uid':0,'gid':0,'mode':0o600,'bytes':len(data),'mtimeNs':1}})
    parents={str(p) for source in snapshot.STATIC for p in source.parents if p!=Path('/')}
    parents.add(str(snapshot.STREAM))
    directories=[{'path':p,'metadata':{'uid':0,'gid':0,'mode':0o700,'bytes':0,'mtimeNs':1}} for p in sorted(parents)]
    logs={}
    for kind in ('host','application'):
        zpath=root/(kind+'.zip');zpath.write_bytes(payloads['normalized/'+kind+'.zip'])
        logs[kind]=snapshot.verify_zip(zpath,kind)
    release = application_release()
    manifest={'format':snapshot.FORMAT,'archive':ARCHIVE,'hostname':snapshot.HOST,'installationInstance':snapshot.INSTANCE,
        'settingsBaseRevision':snapshot.BASE_REVISION,'applicationRevision':release['revision'],
        'apiImage':'sha256:'+'c'*64,'apiImageReference':release['images']['api'],'applicationRelease':release,
        'coverageComplete':False,'gatewayOAuthContinuity':False,'gatewayLiveDatabaseIncluded':False,
        'entries':entries,'directories':directories,'normalizedLogs':logs}
    return manifest,payloads


def make_tar(path,manifest,payloads, *, bad_mode=False, extra_link=False):
    with tarfile.open(path,'w',format=tarfile.USTAR_FORMAT) as archive:
        for index,(name,data) in enumerate({'manifest.json':json.dumps(manifest).encode(),**payloads}.items()):
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644 if bad_mode and index==1 else 0o600
            info.uid=info.gid=0
            archive.addfile(info,io.BytesIO(data))
        if extra_link:
            info=tarfile.TarInfo('unsafe-link');info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd'
            archive.addfile(info)


class ApplicationRuntime(unittest.TestCase):
    def inspect(self, release, *, container_override=None, image_override=None):
        cid = 'd'*64
        reference = release['images']['api']
        image_id = 'sha256:'+'e'*64
        container = {'id':cid,'image':image_id,'imageReference':reference,'user':'node',
                     'state':'running','health':'healthy','startedAt':'2026-11-12T00:00:00.000000000Z',
                     'labels':{'com.docker.compose.project':snapshot.PROJECT,
                               'com.docker.compose.service':'api',
                               'com.docker.compose.project.working_dir':str(snapshot.APP_ROOT),
                               'org.opencontainers.image.revision':release['revision']},
                     'mounts':[{'Destination':'/var/lib/asa-logs','Type':'bind','RW':False,
                                'Source':str(snapshot.APP_ROOT / '.asa/diagnostics/store')},
                               {'Destination':'/var/lib/asa-application-logs','Type':'volume',
                                'Name':snapshot.PROJECT+'_application-logs'}]}
        image = {'id':image_id,'digests':[reference],'os':'linux','architecture':'amd64','revision':release['revision']}
        container.update(container_override or {})
        image.update(image_override or {})
        with mock.patch.object(snapshot, 'stable_read', return_value=(json.dumps(release).encode(), None)) as read, \
                mock.patch.object(snapshot, 'docker_view', return_value=container), \
                mock.patch.object(snapshot, 'run', side_effect=[cid[:12].encode(), json.dumps(image).encode()]) as run:
            result = snapshot.attested_api_runtime()
        read.assert_called_once_with(snapshot.APP_ROOT / '.asa' / 'installed-release.json', 65536)
        self.assertEqual(run.call_args.args[0][-1], reference)
        return result

    def test_accepts_old_and_new_installed_release_with_matching_runtime(self):
        for release in (application_release(snapshot.LEGACY_REVISION, 'asa-deployment-v1'),
                        application_release()):
            with self.subTest(revision=release['revision']):
                self.assertEqual(self.inspect(release)['release'], release)

    def test_rejects_different_runtime_image_even_when_label_matches(self):
        with self.assertRaisesRegex(snapshot.SnapshotError, 'API_IMAGE_DRIFT'):
            self.inspect(application_release(), image_override={'id':'sha256:'+'f'*64})

    def test_rejects_digest_missing_from_local_image(self):
        with self.assertRaisesRegex(snapshot.SnapshotError, 'API_IMAGE_DRIFT'):
            self.inspect(application_release(), image_override={'digests':[]})

    def test_rejects_mutable_container_reference(self):
        with self.assertRaisesRegex(snapshot.SnapshotError, 'API_RUNTIME_CHANGED'):
            self.inspect(application_release(), container_override={'imageReference':'asa-lab-api:local'})

    def test_rejects_foreign_release_manifest_before_docker(self):
        release = application_release()
        release['repository'] = 'foreign/repository'
        with mock.patch.object(snapshot, 'stable_read', return_value=(json.dumps(release).encode(), None)), \
                mock.patch.object(snapshot, 'run') as run:
            with self.assertRaisesRegex(snapshot.SnapshotError, 'API_RELEASE_MANIFEST_INVALID'):
                snapshot.attested_api_runtime()
        run.assert_not_called()


class RestoreContract(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.manifest,self.payloads=fixture(self.root)
        self.output=self.root/'extracted';self.output.mkdir(mode=0o700)
        self.tar=self.root/'snapshot.tar'

    def tearDown(self):
        self.temporary.cleanup()

    def test_full_isolated_sqlite_zip_manifest_pass(self):
        make_tar(self.tar,self.manifest,self.payloads)
        result=snapshot.verify_tar(self.tar,self.output,ARCHIVE)
        self.assertEqual(result['sqliteIntegrity'],dict.fromkeys(snapshot.DB_NAMES,'ok'))
        self.assertFalse(result['livePathsWritten'])
        self.assertFalse(result['coverageComplete'])
        self.assertEqual(result['fileCount'],len(snapshot.STATIC)+5)
        for file in self.output.rglob('*'):
            self.assertFalse(file.is_symlink())
            if file.is_file(): self.assertEqual(file.stat().st_mode&0o777,0o600)

    def test_legacy_archive_still_verifies(self):
        self.manifest.pop('applicationRelease')
        self.manifest.pop('apiImageReference')
        self.manifest.update(format=snapshot.LEGACY_FORMAT, applicationRevision=snapshot.LEGACY_REVISION,
                             apiImage=snapshot.LEGACY_API_IMAGE, canonicalExporterBlob=snapshot.LEGACY_EXPORTER_BLOB)
        make_tar(self.tar,self.manifest,self.payloads)
        self.assertTrue(snapshot.verify_tar(self.tar,self.output,ARCHIVE)['manifestVerified'])

    def test_inconsistent_archive_release_rejected_before_extraction(self):
        self.manifest['apiImageReference'] = 'ghcr.io/'+snapshot.REPOSITORY+'/api@sha256:'+'d'*64
        make_tar(self.tar,self.manifest,self.payloads)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'MANIFEST_IDENTITY'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_required_configuration_rejected(self):
        self.manifest['entries'].pop(0)
        make_tar(self.tar,self.manifest,self.payloads)
        with self.assertRaises(snapshot.SnapshotError): snapshot.verify_tar(self.tar,self.output,ARCHIVE)

    def test_changed_configuration_hash_rejected(self):
        self.manifest['entries'][0]['sha256']='0'*64
        make_tar(self.tar,self.manifest,self.payloads)
        with self.assertRaisesRegex(snapshot.SnapshotError,'TAR_FILE_SHA256'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)

    def test_traversal_manifest_rejected_before_writes(self):
        self.manifest['entries'][0]['member']='files/../../escape'
        make_tar(self.tar,self.manifest,self.payloads)
        with self.assertRaisesRegex(snapshot.SnapshotError,'MANIFEST_PATH'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)
        self.assertEqual(list(self.output.iterdir()),[])
        self.assertFalse((self.root/'escape').exists())

    def test_unreviewed_config_path_rejected(self):
        self.manifest['entries'][0]['member']='files/etc/shadow'
        make_tar(self.tar,self.manifest,self.payloads)
        with self.assertRaisesRegex(snapshot.SnapshotError,'MANIFEST_CONFIG_SCOPE'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)

    def test_symlink_member_rejected(self):
        make_tar(self.tar,self.manifest,self.payloads,extra_link=True)
        with self.assertRaisesRegex(snapshot.SnapshotError,'TAR_MEMBER_METADATA'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)
        self.assertFalse((self.output/'unsafe-link').exists())

    def test_archive_mode_rejected(self):
        make_tar(self.tar,self.manifest,self.payloads,bad_mode=True)
        with self.assertRaisesRegex(snapshot.SnapshotError,'TAR_MEMBER_METADATA'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)

    def test_directory_permissions_manifest_required(self):
        self.manifest['directories']=[]
        make_tar(self.tar,self.manifest,self.payloads)
        with self.assertRaisesRegex(snapshot.SnapshotError,'MANIFEST_DIRECTORY_MISSING'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)

    def test_truncated_tar_rejected(self):
        make_tar(self.tar,self.manifest,self.payloads)
        with tarfile.open(self.tar) as archive:
            last=archive.getmembers()[-1]
            end=last.offset_data+((last.size+511)//512)*512
        self.tar.write_bytes(self.tar.read_bytes()[:end])
        with self.assertRaisesRegex(snapshot.SnapshotError,'TAR_END_MARKER'):
            snapshot.verify_tar(self.tar,self.output,ARCHIVE)

    def test_zip_false_complete_and_extra_members_rejected(self):
        for args in ({'coverage':True},{'extra':True}):
            path=self.root/'bad.zip';path.write_bytes(normalized_zip('host',**args))
            with self.assertRaises(snapshot.SnapshotError): snapshot.verify_zip(path,'host')

    def test_corrupted_zip_bytes_rejected(self):
        path=self.root/'corrupt.zip';path.write_bytes(normalized_zip('host'))
        with zipfile.ZipFile(path) as z:
            item=z.getinfo('records/part-00001.jsonl')
            position=item.header_offset+30+len(item.filename.encode())+len(item.extra)
        data=bytearray(path.read_bytes());data[position+2]^=0x40;path.write_bytes(data)
        with self.assertRaises(Exception): snapshot.verify_zip(path,'host')

    def test_sqlite_online_backup_includes_committed_wal(self):
        source=self.root/'online.db';target=self.root/'online-copy.db'
        writer=sqlite3.connect(source)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE TABLE t(n INTEGER)')
            writer.executemany('INSERT INTO t VALUES(?)',[(n,) for n in range(3000)])
            writer.commit()
            self.assertGreater(Path(str(source)+'-wal').stat().st_size,0)
            with mock.patch.object(snapshot,'trusted',side_effect=lambda p,**kw:Path(p).stat()):
                snapshot.sqlite_snapshot(source,target)
            with sqlite3.connect(target) as copy_db:
                self.assertEqual(copy_db.execute('SELECT COUNT(*),SUM(n) FROM t').fetchone(),(3000,4498500))
        finally:
            writer.close()


class ConfigurationNamespace(unittest.TestCase):
    def test_only_configuration_folder_created_and_no_overwrite(self):
        calls=[]
        state={'created':False}
        class App:
            def __init__(self,token): pass
            def api(self,suffix,*,params=None,method='GET'):
                calls.append((suffix,dict(params or {}),method))
                if suffix=='/resources/upload':
                    return 200,{'method':'PUT','href':'https://fixture.yandex.net/cipher'}
                path=params['path']
                if path=='app:/School-VM100': return 200,{'type':'dir'}
                if method=='PUT': state['created']=True;return 201,{}
                return (200,{'type':'dir'}) if state['created'] else (404,{})
        transport=types.SimpleNamespace(AppFolder=App,validate_signed_link=lambda href:('fixture.yandex.net',443,'/cipher'))
        provider=roundtrip.configuration_provider(transport,'synthetic-non-secret-token')
        provider.folder_preflight()
        provider.signed_url('upload',ARCHIVE)
        self.assertEqual([p['path'] for _,p,m in calls if m=='PUT'],[roundtrip.CONFIG_ROOT])
        self.assertFalse(any('Database-Backups' in p.get('path','') for _,p,_ in calls))
        self.assertEqual(calls[-1][1]['overwrite'],'false')
        with self.assertRaises(snapshot.SnapshotError):
            provider.metadata('asa-school-01-20261108T091500Z-3f6a47b2.tar.age')

    def test_full_download_saved_and_wrong_hash_leaves_no_file(self):
        payload=b'age-encryption.org/v1\n'+b'synthetic-cipher-fixture'*15
        class Response:
            status=200
            def __init__(self): self.body=io.BytesIO(payload)
            def read(self,n): return self.body.read(n)
        class Connection:
            def __init__(self,*a,**kw): pass
            def request(self,*a,**kw): pass
            def getresponse(self): return Response()
            def close(self): pass
        provider=types.SimpleNamespace(signed_url=lambda *args:('fixture.yandex.net',443,'/cipher'))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(roundtrip.http.client,'HTTPSConnection',Connection), \
                mock.patch.object(snapshot,'pinned_transport',return_value=types.SimpleNamespace()):
            target=Path(tmp)/ARCHIVE
            roundtrip.download_configuration(provider,ARCHIVE,target,len(payload),hashlib.sha256(payload).hexdigest())
            self.assertEqual(target.read_bytes(),payload)
            other=Path(tmp)/'wrong.tar.age'
            with self.assertRaisesRegex(snapshot.SnapshotError,'CONFIG_DOWNLOAD_INTEGRITY_FAILED'):
                roundtrip.download_configuration(provider,ARCHIVE,other,len(payload),'0'*64)
            self.assertFalse(other.exists())
            self.assertFalse(other.with_suffix('.age.partial').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
