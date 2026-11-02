import importlib.util
from pathlib import Path
import unittest
import contextlib
import io
import os
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
P=ROOT/"scripts/install-school-nightly-database-backup.py"
spec=importlib.util.spec_from_file_location("school_backup_timer",P)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NightlyTimerTests(unittest.TestCase):
    def test_unknown_or_unmanaged_timer_state_is_rejected_before_any_unit_write(self):
        outputs=('LoadState=loaded\nUnitFileState=masked\nActiveState=active\n',
                 'LoadState=loaded\nUnitFileState=enabled\nActiveState=activating\n',
                 'LoadState=not-found\nActiveState=active\n',
                 'LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n')
        with tempfile.TemporaryDirectory() as temp:
            for output in outputs:
                with self.subTest(output=output), patch.object(module,'TIMER',Path(temp)/'absent.timer'), \
                     patch.object(module,'checked_host'), patch.object(module,'make_directory'), \
                     patch.object(module,'preflight'), patch.object(module,'run',return_value=output), \
                     patch.object(module,'make_unit') as writer:
                    with self.assertRaises(module.InstallError):module.install()
                    writer.assert_not_called()

    def test_partial_new_unit_write_is_removed_without_touching_existing_file(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(module.os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0),create=True):
            target=Path(temp)/'new.service'
            with patch.object(module.os,'fsync',side_effect=OSError('injected fsync')):
                with self.assertRaises(OSError):module.make_unit(target,module.SERVICE_BODY)
            self.assertFalse(target.exists())
            target.write_text('foreign bytes',encoding='utf-8')
            with self.assertRaises(module.InstallError):module.make_unit(target,module.SERVICE_BODY)
            self.assertEqual(target.read_text(encoding='utf-8'),'foreign bytes')

    def test_foreign_timer_conflict_removes_only_the_service_created_by_this_attempt(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            service,timer=Path(temp)/'backup.service',Path(temp)/'backup.timer'
            timer.write_text('foreign timer',encoding='utf-8')
            for name,value in [('SERVICE',service),('TIMER',timer)]:stack.enter_context(patch.object(module,name,value))
            for name in ('checked_host','make_directory','preflight'):stack.enter_context(patch.object(module,name))
            command=stack.enter_context(patch.object(module,'run',return_value='LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n'))
            stack.enter_context(patch.object(module.os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0),create=True))
            with self.assertRaisesRegex(module.InstallError,'EXISTING_SYSTEMD_UNIT_DIFFERENT'):module.install()
            self.assertFalse(service.exists())
            self.assertEqual(timer.read_text(encoding='utf-8'),'foreign timer')
            self.assertFalse(any(call.args[0][1] in ('enable','disable','start','stop') for call in command.call_args_list))

    def install_failure(self, previous, failure, *, existing=True, rollback_failure=False):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            service,timer=Path(temp)/'backup.service',Path(temp)/'backup.timer'
            if existing:
                service.write_text(module.SERVICE_BODY,encoding='utf-8')
                timer.write_text(module.TIMER_BODY,encoding='utf-8')
            state=list(previous or ('disabled','inactive'))
            calls=[]
            failed=False
            def command(args, **_):
                nonlocal failed
                calls.append(args)
                action=args[1]
                if action=='show':
                    return ('LoadState=loaded\nUnitFileState='+state[0]+'\nActiveState='+state[1]+'\n') if timer.exists() else 'LoadState=not-found\nActiveState=inactive\n'
                if action=='enable':
                    state[0]='enabled-runtime' if '--runtime' in args else 'enabled'
                    if '--now' in args:state[1]='active'
                if not failed and action==failure:
                    failed=True
                    raise module.InstallError('INJECTED_'+failure)
                if failed and rollback_failure and action=='disable':
                    raise module.InstallError('INJECTED_ROLLBACK')
                if action=='disable':
                    state[0]='disabled'
                    if '--now' in args:state[1]='inactive'
                if action=='start':state[1]='active'
                if action=='stop':state[1]='inactive'
                if action=='is-enabled':return state[0]
                if action=='is-active':return state[1]
                if action=='list-timers':return timer.name
                return ''
            for name,value in [('SERVICE',service),('TIMER',timer)]:stack.enter_context(patch.object(module,name,value))
            for name in ('checked_host','make_directory','preflight'):stack.enter_context(patch.object(module,name))
            stack.enter_context(patch.object(module,'run',side_effect=command))
            stack.enter_context(patch.object(module.os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0),create=True))
            stderr=stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            with self.assertRaisesRegex(module.InstallError,'INJECTED_'+failure):module.install()
            if existing:
                self.assertEqual(service.read_text(encoding='utf-8'),module.SERVICE_BODY)
                self.assertEqual(timer.read_text(encoding='utf-8'),module.TIMER_BODY)
            else:
                self.assertFalse(service.exists());self.assertFalse(timer.exists())
            return tuple(state),calls,stderr.getvalue()

    def test_failed_reconcile_never_disables_or_stops_untouched_running_timer(self):
        for failure in ('daemon-reload','is-enabled','is-active','list-timers'):
            with self.subTest(failure=failure):
                state,calls,_=self.install_failure(('enabled','active'),failure)
                self.assertEqual(state,('enabled','active'))
                self.assertFalse(any(c[1] in ('enable','disable','start','stop') for c in calls))

    def test_failed_activation_restores_each_existing_enablement_and_activity(self):
        for enabled in ('enabled','disabled','enabled-runtime'):
            for active in ('active','inactive'):
                if (enabled,active)==('enabled','active'):continue
                for failure in ('enable','is-enabled','is-active','list-timers'):
                    with self.subTest(enabled=enabled,active=active,failure=failure):
                        state,_,stderr=self.install_failure((enabled,active),failure)
                        self.assertEqual(state,(enabled,active))
                        self.assertNotIn('ROLLBACK_INCOMPLETE',stderr)

    def test_new_units_only_are_removed_after_failed_install(self):
        for failure in ('daemon-reload','enable','is-enabled','list-timers'):
            with self.subTest(failure=failure):
                _,calls,_=self.install_failure(None,failure,existing=False)
                disabled=any(c[1]=='disable' for c in calls)
                self.assertEqual(disabled,failure!='daemon-reload')

    def test_rollback_failure_preserves_original_error_and_reports_incomplete(self):
        _,_,stderr=self.install_failure(('disabled','inactive'),'enable',rollback_failure=True)
        self.assertIn('NIGHT_TIMER_ROLLBACK_INCOMPLETE=YES',stderr)

    def test_daily_night_moscow_no_catchup(self):
        self.assertIn("OnCalendar=*-*-* 03:20:00 Europe/Moscow",module.TIMER_BODY)
        self.assertIn("Persistent=false",module.TIMER_BODY)
        self.assertIn("RandomizedDelaySec=0",module.TIMER_BODY)
        self.assertIn("Unit=asa-school-nightly-database-backup.service",module.TIMER_BODY)
        self.assertNotIn("OnBootSec=",module.TIMER_BODY)
        self.assertNotIn("Persistent=true",module.TIMER_BODY)

    def test_service_uses_only_pinned_school_backups(self):
        self.assertIn("User=root",module.SERVICE_BODY)
        self.assertIn("WorkingDirectory=/srv/asa-lab",module.SERVICE_BODY)
        self.assertIn("school-nightly-database-backup.py --apply",module.SERVICE_BODY)
        self.assertIn("TimeoutStartSec=infinity",module.SERVICE_BODY)
        self.assertIn("UMask=0077",module.SERVICE_BODY)
        self.assertNotIn("docker compose down -v",module.SERVICE_BODY)
        self.assertEqual(module.ROOT,Path("/var/backups/asa-school-nightly"))

    def test_manual_shortcut_is_one_command_without_night_guard(self):
        manual=ROOT/"scripts/asa-school-backup-now.sh"
        script=manual.read_text(encoding="utf-8")
        self.assertIn("ACTION=--run",script)
        self.assertIn('exec /usr/bin/python3 "$BACKUP" --apply --manual',script)
        self.assertIn('exec /usr/bin/python3 "$BACKUP" --preflight',script)
        self.assertIn('exec /usr/bin/python3 "$CLOUD" status',script)
        self.assertNotIn("03:00-05:00",script)

    def test_manual_install_does_not_restart_or_modify_timer(self):
        installer=(ROOT/"scripts/install-school-manual-backup.py").read_text(encoding="utf-8")
        self.assertIn("NIGHT_TIMER_UNMODIFIED=YES",installer)
        self.assertIn("LIVE_DATABASE_BACKUP_NOT_TRIGGERED_DURING_INSTALL=YES",installer)
        self.assertIn("O_EXCL",installer)
        self.assertNotIn('systemctl", "restart"',installer)
        self.assertNotIn('systemctl", "start"',installer)

    def test_timers_not_run_from_preflight_or_dryrun(self):
        text=P.read_text(encoding="utf-8")
        self.assertIn("if args.apply:",text)
        self.assertIn("run([\"/usr/bin/python3\",str(SCRIPT),\"--preflight\"]",text)
        self.assertIn("systemctl\",\"enable\",\"--now\",TIMER.name",text)
        self.assertNotIn("systemctl\",\"start\",SERVICE.name",text)


if __name__=="__main__":
    unittest.main()
