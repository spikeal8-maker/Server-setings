"""Real browser acceptance on dedicated owner-authorized test identities only.

No test mocks, certificate bypass, private account import or student-work edits.
Fixtures are retained privately for inspection and reuse after a dropped session.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid


def main():
    from playwright.sync_api import sync_playwright
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    args = parser.parse_args()
    origin, revision = os.environ['ASA_ACCEPTANCE_ORIGIN'], os.environ['ASA_ACCEPTANCE_REVISION']
    fixture_path = Path(args.fixture)
    fixture_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixture = json.loads(fixture_path.read_text()) if fixture_path.exists() else None
    if fixture and fixture['origin'] != origin:
        raise RuntimeError('Fixture belongs to a different entry origin')
    if not fixture:
        ident = uuid.uuid4().hex[:16]
        fixture = {'origin': origin, 'email': 'asa-ops-'+ident+'@example.invalid',
                   'username': 'asaops_'+ident, 'password': secrets.token_urlsafe(32),
                   'projects': {}, 'authorized_test_fixture': True}
    def save():
        temporary = fixture_path.with_name(fixture_path.name+'.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(fixture, f);f.flush();os.fsync(f.fileno())
        os.replace(temporary, fixture_path)
    save()
    checks = {k: False for k in ('teacher_login', 'student_login', 'course_open', 'existing_project_open',
                                 'scratch_save_reopen', 'electronics_save_reopen')}
    report = {'revision': revision, 'entry_origin': origin, 'checks': checks, 'error': None}
    stage = 'browser_launch'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(base_url=origin)  # strict TLS default
        context.set_default_timeout(45000)
        page = context.new_page()
        def api(method, path, body=None, allowed=(200, 201)):
            response = context.request.fetch(origin+path, method=method, data=body,
                       headers={'Origin': origin, 'Idempotency-Key': str(uuid.uuid4())})
            if response.status not in allowed:
                # Error details may include identifiers. Return only status/stage.
                raise RuntimeError('API_STATUS_'+str(response.status))
            return response.json()
        try:
            stage = 'release_provenance'
            ready = api('GET', '/health/ready')
            if ready['deployment']['revision'] != revision:
                raise RuntimeError('REVISION_DRIFT')
            stage = 'teacher_registration_or_login'
            if not fixture.get('registered'):
                api('POST', '/api/auth/register', {'email': fixture['email'], 'username': fixture['username'],
                    'password': fixture['password'], 'displayName': 'ASA Operations Test Teacher',
                    'birthDate': '1990-04-12', 'country': 'RU'})
                fixture['registered'] = True;save()
            else:
                # Ordinary auth API; never forge bot proofs or disable a challenge.
                api('POST', '/api/auth/login', {'identifier': fixture['email'], 'password': fixture['password']})
            api('GET', '/api/auth/me')
            page.goto(origin+'/#/home', wait_until='domcontentloaded')
            page.locator('body').wait_for()
            checks['teacher_login'] = True
            stage = 'test_classroom'
            if not fixture.get('classroom'):
                api('POST', '/api/capabilities/educator/self-attest', {}, allowed=(200, 201))
                cls = api('POST', '/api/classrooms', {'title': 'ASA Operations Acceptance (TEST)',
                         'ageBand': 'mixed', 'topicKeys': [], 'safeModeDefault': True})['classroom']
                fixture['classroom'] = cls['id'];save()
            if not fixture.get('join_code'):
                detail = api('GET', '/api/classrooms/'+fixture['classroom'])['classroom']
                fixture['join_code'] = detail['joinCode'];save()
            if not fixture.get('student_code'):
                seat = api('POST', '/api/classrooms/'+fixture['classroom']+'/seats',
                           {'displayLabel': 'ASA Operations Test Student', 'safeMode': True})['student']
                fixture['student_code'] = seat['studentCode'];save()
            stage = 'student_login'
            learner = browser.new_context(base_url=origin)
            response = learner.request.post(origin+'/api/class-join/studentseat',
                         data={'code': fixture['join_code'], 'studentCode': fixture['student_code']}, headers={'Origin': origin})
            if response.status != 200:
                raise RuntimeError('STUDENT_LOGIN_STATUS_'+str(response.status))
            learner_page = learner.new_page();learner_page.goto(origin+'/#/home', wait_until='domcontentloaded')
            checks['student_login'] = True
            learner.close()
            stage = 'course_open'
            courses = api('GET', '/api/courses')
            items = courses.get('items', [])
            # Opening a real existing course is read-only. Absence is not a pass.
            if items:
                course_id = items[0]['id']
                api('GET', '/api/courses/'+course_id+'/outline')
                page.goto(origin+'/#/learning/'+course_id, wait_until='domcontentloaded')
                checks['course_open'] = True
            for module, check in [('blocks', 'scratch_save_reopen'), ('electronics', 'electronics_save_reopen')]:
                stage = module+'_project'
                if module not in fixture['projects']:
                    created = api('POST', '/api/projects', {'scope': 'personal', 'classroomId': None,
                                   'module': module, 'title': 'ASA Operations '+module+' (TEST)'})
                    fixture['projects'][module] = created['project']['id'];save()
                project = fixture['projects'][module]
                before = api('GET', '/api/projects/'+project)
                page.goto(origin+'/#/home/'+project, wait_until='domcontentloaded')
                if module == 'blocks':
                    frame = page.frame_locator('iframe[title="Scratch runtime"]')
                    field = frame.get_by_placeholder('x', exact=True)
                    field.wait_for()
                    value = str(secrets.randbelow(120)+20)
                    field.fill(value);field.press('Enter')
                    reopened_field = lambda target: target.frame_locator('iframe[title="Scratch runtime"]').get_by_placeholder('x', exact=True)
                else:
                    # Change through the actual editor UI and wait for its real PUT.
                    editor = page.locator('.workbench-stage');editor.wait_for()
                    source = page.locator('.arduino-source-editor textarea')
                    if not source.is_visible():
                        page.get_by_role('button', name='Открыть редактор кода', exact=True).click()
                    value = '// ASA Operations acceptance '+uuid.uuid4().hex+'\nvoid setup(){}\nvoid loop(){}\n'
                    source.fill(value)
                    reopened_field = lambda target: target.locator('.arduino-source-editor textarea')
                stage = module+'_save'
                # Poll the server, never accept a green UI badge as saved evidence.
                import time
                deadline = time.monotonic()+100
                initial_revision = before.get('draft', {}).get('revision')
                while time.monotonic() < deadline:
                    after = api('GET', '/api/projects/'+project)
                    if after.get('draft', {}).get('revision') != initial_revision:
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError('SAVE_NOT_OBSERVED')
                fresh = browser.new_context(base_url=origin)
                fresh.add_cookies(context.cookies())  # no localStorage/recovery cache
                reopened = fresh.new_page();reopened.goto(origin+'/#/home/'+project, wait_until='domcontentloaded')
                if module == 'electronics' and not reopened_field(reopened).is_visible():
                    reopened.get_by_role('button', name='Открыть редактор кода', exact=True).click()
                actual = reopened_field(reopened).input_value(timeout=45000)
                if actual != value:
                    raise RuntimeError('REOPEN_VALUE_MISMATCH')
                reread = fresh.request.get(origin+'/api/projects/'+project).json()
                if reread.get('draft') != after.get('draft'):
                    raise RuntimeError('REOPEN_DRAFT_MISMATCH')
                checks[check] = True
                checks['existing_project_open'] = True
                fresh.close()
        except Exception as exc:
            code = str(exc) if re.fullmatch(r'API_STATUS_[0-9]{3}', str(exc)) else type(exc).__name__
            report['error'] = {'stage': stage, 'code': code}
            try:
                page.screenshot(path=str(fixture_path.parent/'browser-acceptance-failure.png'))
            except Exception:
                pass
        finally:
            context.close();browser.close()
    print(json.dumps(report))
    return 0 if all(checks.values()) else 2


if __name__ == '__main__':
    sys.exit(main())
