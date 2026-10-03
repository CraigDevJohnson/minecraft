"""Exercise protected receipts using only the canonical installed runtime modules."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_runtime_install as fixture_module

ROOT = Path(__file__).resolve().parents[1]

PROTECTED_RECEIPT = r'''
import importlib,json,sys
from pathlib import Path
installed,root=map(Path,sys.argv[1:])
sys.path.insert(0,str(installed))
for name in ('minecraft_instance','minecraft_content','minecraft_access','minecraft_backup','minecraft_backup_store','minecraft_restore'):
    importlib.import_module(name)
import minecraft_backup
sys.path.insert(0,str(root/'tests'))
import test_recovery_receipts
fixture=test_recovery_receipts.RecoveryReceiptTests();fixture.setUp()
# The only external boundary is an in-memory versioned S3 service.
store=fixture.store
tags={}
store.version_tags=lambda key,version:dict(tags.get((key,version),{}))
store.set_retention=lambda key,version,state:tags.__setitem__((key,version),{'minecraft-retention':state})
store.lifecycle_configuration=lambda:{'Rules':[
    {'ID':'retire-verified-survival','Status':'Enabled',
     'Filter':{'And':{'Prefix':'instances/survival/','Tags':[{'Key':'minecraft-retention','Value':'retired'}]}},
     'Expiration':{'Days':14},'NoncurrentVersionExpiration':{'NoncurrentDays':14}},
    {'ID':'abort-incomplete-survival','Status':'Enabled','Filter':{'Prefix':'instances/survival/'},
     'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}},
    {'ID':'remove-expired-markers-survival','Status':'Enabled','Filter':{'Prefix':'instances/survival/'},
     'Expiration':{'ExpiredObjectDeleteMarker':True}}]}
sys.path=[entry for entry in sys.path if Path(entry or '.').resolve()!=root/'scripts']
try:
    result=minecraft_backup.verify_stop_receipt(fixture.source,fixture.drain(),fixture.journal,fixture.bootstrap,
                                               store=store,protect=True,fresh_drain=fixture.drain)
    assert result['survival']['receipt_id']==result['survival']['receipt']['receipt_id']
    assert len(tags)==4 and all(value=={'minecraft-retention':'keep'} for value in tags.values())
    print(json.dumps({'protected':True,'protected_versions':len(tags)}))
except ModuleNotFoundError as error:
    if error.name!='minecraft_retention':raise
    print(json.dumps({'protected':False,'error_category':'missing_retention_module'}))
finally:fixture.doCleanups()
'''


class InstalledRetentionTests(unittest.TestCase):
    def install(self, root):
        fixture = fixture_module.RuntimeInstallTests()
        _, script, environment, _ = fixture.fixture(root)
        result = fixture.run_installer(script, environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root / 'opt/minecraft/scripts'

    def verify(self, installed, root):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', PROTECTED_RECEIPT, str(installed), str(ROOT)],
                                cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_canonical_installed_runtime_protects_all_four_exact_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = self.install(root)
            self.assertEqual((installed / 'minecraft_retention.py').read_bytes(), (ROOT / 'scripts/minecraft_retention.py').read_bytes())
            self.assertEqual(self.verify(installed, root), {'protected': True, 'protected_versions': 4})

    def test_prior_six_module_inventory_reproduces_the_conditional_import_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = self.install(root)
            (installed / 'minecraft_retention.py').unlink()
            self.assertEqual(self.verify(installed, root), {'protected': False, 'error_category': 'missing_retention_module'})


if __name__ == '__main__':
    unittest.main()
