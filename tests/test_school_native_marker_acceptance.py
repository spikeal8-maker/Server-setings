"""Local synthetic proof checks. No native HTTP, Docker, credentials or VM calls."""
from contextlib import closing
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('marker_candidate_test', HERE.parent / 'scripts/school-native-marker-acceptance.py')
marker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marker)
CRON = SimpleNamespace(program=lambda kind: 'asa-school-native-' + kind)


def native():
    return {kind: {'status': 'STAGED_GATE_VERIFIED_NO_BACKUP', 'dispatchId': digit * 32,
                  'kind': kind, 'generation': '3' * 32, 'programSha256': marker.CRON_SHA,
                  'startedAt': '2026-11-08T13:00:00.100000+00:00',
                  'finishedAt': '2026-11-08T13:00:00.123456+00:00',
                  'timestamp': '2026-11-08T13:00:00.123456+00:00', 'level': 'INFO',
                  'backupExecuted': False, 'supervision': '1Panel Supervisor', 'restoreValidated': False}
            for kind, digit in (('database', '1'), ('settings', '2'))}


def rows():
    return [{'id': digit * 64, 'source': marker.SOURCE, 'module': 'system', 'level': 'info',
             'time': '2026-11-08T13:00:00.123Z', 'origin': CRON.program(kind) + '.out.log',
             'message': json.dumps(native()[kind], separators=(',', ':'))}
            for kind, digit in (('database', 'a'), ('settings', 'b'))]


class MarkerDelivery(unittest.TestCase):
    def check_archive(self, values):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'host.zip'
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps({'catalog': {'segments': [{'file': 'records/part-00001.jsonl'}]}}))
                archive.writestr('records/part-00001.jsonl', ''.join(json.dumps(row) + '\n' for row in values))
            return marker.host_markers(path, native(), CRON)

    def test_two_owned_occurrences_survive_json_whitespace(self):
        values = rows()
        values[1]['message'] = json.dumps(native()['settings'], indent=2)
        values.append({**values[0], 'id': 'c' * 64, 'source': 'other-source'})
        self.assertEqual(self.check_archive(values), {'database': 'a' * 64, 'settings': 'b' * 64})

    def test_missing_second_marker_does_not_pass(self):
        with self.assertRaisesRegex(marker.MarkerStop, 'NORMALIZED_MARKER_MISSING'):
            self.check_archive(rows()[:1])

    def test_duplicate_occurrence_does_not_pass_even_with_same_id(self):
        values = rows()
        values.append(deepcopy(values[0]))
        with self.assertRaisesRegex(marker.MarkerStop, 'NORMALIZED_MARKER_DUPLICATED'):
            self.check_archive(values)

    def test_changed_native_object_does_not_pass(self):
        values = rows()
        body = json.loads(values[0]['message'])
        body['backupExecuted'] = True
        values[0]['message'] = json.dumps(body)
        with self.assertRaisesRegex(marker.MarkerStop, 'NORMALIZED_MARKER_CHANGED'):
            self.check_archive(values)

    def test_wrong_normalized_time_origin_or_classification_does_not_pass(self):
        for field, value in [('time', '2026-11-08T13:00:01.123Z'), ('origin', 'other.out.log'),
                             ('module', 'portal'), ('level', 'error')]:
            with self.subTest(field=field):
                values = rows()
                values[0][field] = value
                with self.assertRaisesRegex(marker.MarkerStop, 'NORMALIZED_MARKER_CHANGED'):
                    self.check_archive(values)

    def sqlite(self, second):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'index.sqlite'
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE events(id BLOB UNIQUE, published INTEGER)')
                writer.executemany('INSERT INTO events VALUES(?,?)', [(bytes.fromhex('a' * 64), 1),
                                                                    (bytes.fromhex('b' * 64), second)])
                writer.commit()
                before = list(writer.execute('SELECT id,published FROM events ORDER BY id'))
                try:
                    with patch.object(marker, 'INDEX', path), patch.object(marker, 'trusted', side_effect=lambda value: Path(value).stat()):
                        marker.published({'database': 'a' * 64, 'settings': 'b' * 64})
                finally:
                    self.assertEqual(list(writer.execute('SELECT id,published FROM events ORDER BY id')), before)

    def test_two_published_index_ids_read_without_row_mutation(self):
        self.sqlite(1)

    def test_unpublished_index_id_does_not_pass_and_connection_closes(self):
        with self.assertRaisesRegex(marker.MarkerStop, 'NORMALIZED_EVENT_NOT_PUBLISHED'):
            self.sqlite(0)

    def test_binding_rejects_reused_dispatch_and_boolean_record_id(self):
        binding = {'runId': 'xlog-native-marker-20261108t130000z-' + '4' * 16,
                   'hostSourceRevision': '5' * 40, 'generation': '3' * 32,
                   'jobs': {kind: {'id': n, 'recordId': n + 10, 'dispatchId': str(n) * 32,
                                   'acceptedAt': '2026-11-08T13:00:01+00:00'}
                            for n, kind in enumerate(marker.KINDS, 1)}}
        marker.validate_binding(binding)
        invalid = deepcopy(binding)
        invalid['jobs']['database']['recordId'] = True
        with self.assertRaisesRegex(marker.MarkerStop, 'ACCEPTED_PROBE_BINDING'):
            marker.validate_binding(invalid)
        invalid = deepcopy(binding)
        invalid['jobs']['settings']['dispatchId'] = invalid['jobs']['database']['dispatchId']
        with self.assertRaisesRegex(marker.MarkerStop, 'DISTINCT_PROBES_REQUIRED'):
            marker.validate_binding(invalid)


def old_marker(value):
 value=deepcopy(value)
 value.update(generation='e'*32,dispatchId='f'*32,
  programSha256='6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2')
 return value


def native_fixture(duplicate_current=False):
 current=native()
 binding={'runId':'xlog-native-marker-20261108t130000z-1234567890abcdef',
  'hostSourceRevision':'17d937c01660025e6b5030f44bcb0d776e7c79aa','generation':'3'*32,'jobs':{}}
 state={'phase':'STAGED','owner':'legacy','originalOwner':'legacy','clockTimeZone':'Europe/Moscow',
  'backupExecutedDuringInstall':False,'generation':binding['generation'],'programSha256':marker.CRON_SHA,
  'createdAt':'2026-11-08T12:59:00+00:00','jobs':{}}
 for identity,kind in enumerate(marker.KINDS,1):
  expected={'id':identity,'recordId':identity+10,'dispatchId':current[kind]['dispatchId'],
   'acceptedAt':'2026-11-08T13:00:02+00:00'}
  binding['jobs'][kind]=expected
  state['jobs'][kind]={'id':identity,'probe':{**expected,'status':'NATIVE_GATED_PROBE_PASS',
   'programSha256':marker.CRON_SHA,'backupExecuted':False,'nativeProcessLogObserved':True}}
 class Controller:
  def __init__(self,panel,legacy):pass
  def objects(self,actual,**kwargs):
   assert actual==state and kwargs=={'status':'Disable','idle':True}
 class Panel:
  def records(self,identity,created):
   assert created==state['createdAt']
   return [{'id':identity+10,'cronjobID':identity,'status':'Success'}]
  def request(self,path,payload):
   assert path=='/hosts/tool/supervisor/process/file/get' and payload['file']=='out.log'
   kind=next(kind for kind in marker.KINDS if CRON.program(kind)==payload['name'])
   old=json.dumps(old_marker(current[kind]));own=json.dumps(current[kind])
   return '\n'.join(['synthetic unrelated retained line',old,old,own]+([own] if duplicate_current else []))+'\n'
 cron=SimpleNamespace(load_state=lambda:state,Controller=Controller,Legacy=lambda:None,
  program=CRON.program,runtime_state=lambda kind:current[kind])
 return cron,Panel(),binding,current


def old_normalized_rows():
 values=rows();old=deepcopy(values[0]);old['message']=json.dumps(old_marker(native()['database']))
 old['id']='c'*64;second=deepcopy(old);second['id']='d'*64
 return [old,second,*values]


class RetainedHistoryScope(unittest.TestCase):
 def test_native_old_double_terminal_does_not_count_for_new_dispatch(self):
  cron,panel,binding,current=native_fixture()
  self.assertEqual(marker.native_markers(cron,panel,binding),current)
 def test_native_current_dispatch_duplicate_still_stops(self):
  cron,panel,binding,_=native_fixture(duplicate_current=True)
  with self.assertRaisesRegex(marker.MarkerStop,'NATIVE_JSON_MARKER_NOT_UNIQUE_OR_CHANGED'):
   marker.native_markers(cron,panel,binding)
 def test_normalized_old_double_terminal_does_not_count_for_new_dispatch(self):
  self.assertEqual(MarkerDelivery().check_archive(old_normalized_rows()),
   {'database':'a'*64,'settings':'b'*64})
 def test_normalized_current_dispatch_duplicate_still_stops(self):
  values=old_normalized_rows();values.append(deepcopy(rows()[0]))
  with self.assertRaisesRegex(marker.MarkerStop,'NORMALIZED_MARKER_DUPLICATED'):
   MarkerDelivery().check_archive(values)



if __name__ == '__main__':
    unittest.main()
