"""VM100 encrypted GFS schedules, age transport and retention safety (no cloud)."""
import datetime as dt
import importlib.util
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(SCRIPTS))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,SCRIPTS/path)
    subject=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    return subject

gfs=load("gfs_school","school-gfs-backup.py")
installer=load("gfs_installer","install-school-gfs-backup.py")
switcher=load("gfs_switcher","switch-school-gfs-release.py")


class FakeFolder:
    def __init__(self, items, *, total=None, status=200, delete_status=204):
        self.items=items
        self.total=len(items) if total is None else total
        self.status=status
        self.delete_status=delete_status
        self.deletes=[]
    def api(self, suffix, *, params=None, method="GET"):
        assert suffix=="/resources"
        if method=="DELETE":
            self.deletes.append(params["path"])
            return self.delete_status,{}
        return self.status,{"_embedded":{"items":self.items,"total":self.total}}


def entry(i, prefix="asa-school-01-", suffix=".tar.age"):
    return {"name":prefix+f"202611{i:02d}T002000Z-"+f"{i:08x}"+suffix,
            "type":"file"}


class SchoolGfsSafetyTests(unittest.TestCase):
    def test_tiers_hourly_only(self):
        for day in (1,3,8,15,30):
            date=dt.datetime(2026,11,day,12,0,tzinfo=dt.timezone.utc)
            self.assertEqual(gfs.tiers_for("hourly",date),["hourly-db"])

    def test_tiers_daily_weekly_monthly(self):
        ordinary=dt.datetime(2026,11,3,0,20,tzinfo=gfs.MOSCOW)
        sunday=dt.datetime(2026,11,8,0,20,tzinfo=gfs.MOSCOW)
        first_sunday=dt.datetime(2026,11,1,0,20,tzinfo=gfs.MOSCOW)
        self.assertEqual(gfs.tiers_for("full",ordinary),["daily-full"])
        self.assertEqual(gfs.tiers_for("full",sunday),["daily-full","weekly-full"])
        self.assertEqual(gfs.tiers_for("full",first_sunday),["daily-full","weekly-full","monthly-full"])

    def test_unexpected_kind_is_not_accepted(self):
        with self.assertRaisesRegex(gfs.GfsError,"UNKNOWN_BACKUP_KIND"):
            gfs.tiers_for("settings",dt.datetime.now(dt.timezone.utc))

    def test_hourly_retention_is_exactly_24(self):
        files=[entry(i) for i in range(1,31)]
        holder=FakeFolder(files)
        to_remove=gfs.prune_remote(holder,"hourly-db",apply=False)
        self.assertEqual(len(to_remove),6)
        self.assertEqual(holder.deletes,[])
        self.assertTrue(to_remove[0].startswith("asa-school-01-20261101"))

    def test_daily_weekly_monthly_retention(self):
        for tier,limit in gfs.RETENTION.items():
            holder=FakeFolder([entry(i) for i in range(1,31)])
            old=gfs.prune_remote(holder,tier,apply=False)
            self.assertEqual(len(old),30-limit)

    def test_remote_pruning_never_touches_unrelated_files(self):
        items=[entry(i) for i in range(1,27)]
        items.extend([{"name":"owner-note.txt","type":"file"},
                      {"name":"other-school-20261101T002000Z-12345678.tar.age","type":"file"},
                      {"name":"subfolder","type":"dir"}])
        holder=FakeFolder(items)
        removed=gfs.prune_remote(holder,"hourly-db",apply=True)
        self.assertEqual(len(removed),2)
        self.assertEqual(len(holder.deletes),2)
        self.assertTrue(all(x.startswith(gfs.REMOTE+"/hourly-db/asa-school-01-") for x in holder.deletes))
        self.assertFalse(any("owner-note.txt" in x or "other-school" in x for x in holder.deletes))

    def test_bad_listing_or_pagination_denies_prune(self):
        for status,total in ((403,None),(200,1001)):
            holder=FakeFolder([entry(1)],total=total,status=status)
            with self.subTest(status=status,total=total),self.assertRaises(gfs.GfsError):
                gfs.prune_remote(holder,"daily-full",apply=True)
            self.assertFalse(holder.deletes)

    def test_remote_delete_failure_blocks_job(self):
        holder=FakeFolder([entry(i) for i in range(1,27)],delete_status=403)
        with self.assertRaisesRegex(gfs.GfsError,"DELETE_UNCONFIRMED"):
            gfs.prune_remote(holder,"hourly-db",apply=True)

    def test_local_retention_only_managed_encrypted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            dest=Path(directory)/"hourly"
            dest.mkdir()
            items=[dest/entry(i)["name"] for i in range(1,12)]
            for p in items:
                p.write_bytes(b"age-encryption.org/v1\n")
                p.chmod(0o600)
            unmanaged=dest/"private-archive.txt"
            unmanaged.write_text("owner data",encoding="utf-8")
            with patch.object(gfs,"ARCHIVES",Path(directory)):
                gfs.local_retention("hourly")
            remaining=sorted(p.name for p in dest.glob("*.age"))
            self.assertEqual(len(remaining),gfs.LOCAL_KEEP["hourly"])
            self.assertEqual(remaining,sorted(p.name for p in items[-6:]))
            self.assertTrue(unmanaged.exists())

    def test_installer_calendar_and_no_catchup(self):
        units=installer.unit_texts()
        self.assertEqual(len(units),4)
        hourly=units["asa-school-gfs-hourly.timer"]
        full=units["asa-school-gfs-full.timer"]
        self.assertIn("OnCalendar=*-*-* *:45:00 Europe/Moscow",hourly)
        self.assertIn("OnCalendar=*-*-* 00:20:00 Europe/Moscow",full)
        for unit in (hourly,full):
            self.assertIn("Persistent=false",unit)
        for kind in ("hourly","full"):
            service=units[f"asa-school-gfs-{kind}.service"]
            self.assertIn("Type=oneshot",service)
            self.assertIn("--kind "+kind+" --apply",service)
            self.assertNotIn("docker compose up",service)
            self.assertNotIn("DROP DATABASE",service)

    def test_pg_archive_verification_can_read_root_owned_docker_copy(self):
        source=(SCRIPTS/"school-gfs-backup.py").read_text(encoding="utf-8")
        self.assertIn('["docker", "exec", "--user", "0", POSTGRES, "pg_restore", "--list"',source)
        self.assertIn('["docker", "exec", "--user", "0", POSTGRES, "rm", "-f"',source)

    def test_pinned_release_upgrade_changes_only_exact_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)/"releases"
            old, new = "a"*40, "b"*40
            for sha in (old,new):
                release=base/sha
                release.mkdir(parents=True)
                shutil.copy2(SCRIPTS/"install-school-gfs-backup.py",
                             release/"install-school-gfs-backup.py")
                for name in ("school-gfs-backup.py", "school-nightly-database-backup.py",
                             "school_yandex_app_folder.py", "cloud_storage.py"):
                    (release/name).write_text("pass\n",encoding="utf-8")
            with patch.object(switcher,"BASE",base):
                original=switcher.load_units(old)
                replacement=switcher.load_units(new)
            for kind in ("hourly","full"):
                service=f"asa-school-gfs-{kind}.service"
                self.assertEqual(
                    replacement[service],
                    original[service].replace("/releases/"+old+"/","/releases/"+new+"/"))
                self.assertEqual(
                    replacement[f"asa-school-gfs-{kind}.timer"],
                    original[f"asa-school-gfs-{kind}.timer"])

    def test_atomic_unit_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            target=base/"actual.service"
            target.write_text("original",encoding="utf-8")
            link=base/"link.service"
            link.symlink_to(target)
            with patch.object(switcher,"UNIT_DIR",base):
                with self.assertRaisesRegex(switcher.SwitchError,"SERVICE_UNIT_DISAPPEARED"):
                    switcher.atomic_unit(link,"altered")
                self.assertEqual(target.read_text(),"original")

    def test_source_switch_success_does_not_double_close_lock_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            receipts=base/"receipts"
            receipts.mkdir()
            changes={}
            for kind in ("hourly","full"):
                path=base/f"asa-school-gfs-{kind}.service"
                path.write_text("old",encoding="utf-8")
                changes[kind]=(path,"old","new")
            old_sha,new_sha="a"*40,"b"*40
            def fake_run(args, timeout=35):
                if args[:2]==["systemctl","show"]:
                    return "ExecStart=/usr/bin/python3 /opt/asa-school-gfs/releases/"+new_sha+"/runner"
                if args[:2]==["systemctl","is-enabled"]:
                    return "enabled"
                if args[:2]==["systemctl","is-active"]:
                    return "active"
                return ""
            with patch.object(switcher,"LOCK",base/"lock"), \
                 patch.object(switcher,"UNIT_DIR",base), \
                 patch.object(switcher,"RECEIPT",receipts/"latest-source-switch.json"), \
                 patch.object(switcher,"preflight",return_value=changes), \
                 patch.object(switcher,"run",side_effect=fake_run):
                switcher.apply(old_sha,new_sha)
            self.assertTrue((base/"lock").is_file())
            self.assertEqual((receipts/"latest-source-switch.json").stat().st_mode & 0o777,0o600)
            for path,_,new in changes.values():
                self.assertEqual(path.read_text(),new)

    def test_no_unencrypted_uploads_in_source(self):
        code=(SCRIPTS/"school-gfs-backup.py").read_text(encoding="utf-8")
        self.assertIn("age_encrypt(stage, saved, archive)",code)
        self.assertIn("target_provider.upload_and_verify(archive)",code)
        self.assertIn('"remoteFullReadbackSha256": True',code)
        self.assertNotIn("rclone copyto",code)


if __name__=="__main__":
    unittest.main()
