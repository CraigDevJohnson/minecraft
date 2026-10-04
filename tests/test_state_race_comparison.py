import contextlib
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import minecraft_on_demand as lifecycle
from minecraft_host import HostError
import test_on_demand_host as normal_tests
import test_s3_session as s3_tests


def checks():
    return [{'config_addr': 'var.z', 'status': 'pass', 'objects': [
                {'object_addr': 'var.z[1]', 'status': 'pass', 'failure_messages': []},
                {'object_addr': 'var.z[0]', 'status': 'pass', 'failure_messages': []}]},
            {'config_addr': 'var.a', 'status': 'pass', 'objects': [
                {'object_addr': 'var.a', 'status': 'pass', 'failure_messages': []}]}]


def reorder(state):
    state['check_results'].reverse()
    for item in state['check_results']:item['objects'].reverse()


class StateRaceComparisonTests(unittest.TestCase):
    def setUp(self):
        self.state = {**normal_tests.state(), 'check_results': checks(),
                      'terraform_version': '1.12.6', 'outputs': {'deployment': {'sensitive': True, 'value': {'present': True}}}}

    def test_only_config_and_object_order_is_canonicalized_without_mutating_operands(self):
        changed = copy.deepcopy(self.state);reorder(changed)
        original = copy.deepcopy(self.state);permuted = copy.deepcopy(changed)
        self.assertNotEqual(self.state, changed)
        self.assertTrue(lifecycle.state_matches(self.state, changed))
        self.assertEqual(self.state, original);self.assertEqual(changed, permuted)

    def test_lineage_serial_resources_outputs_status_messages_and_other_arrays_stay_exact(self):
        for field in ('lineage', 'serial', 'resource', 'resource-order', 'output', 'status', 'message', 'version', 'extra'):
            changed = copy.deepcopy(self.state);reorder(changed)
            if field == 'lineage':changed['lineage'] = 'another-lineage'
            elif field == 'serial':changed['serial'] += 1
            elif field == 'resource':changed['resources'][0]['instances'][0]['attributes']['id'] = 'another-volume'
            elif field == 'resource-order':changed['resources'].reverse()
            elif field == 'output':changed['outputs']['deployment']['value']['present'] = False
            elif field == 'status':changed['check_results'][0]['status'] = 'fail'
            elif field == 'message':changed['check_results'][0]['objects'][0]['failure_messages'] = ['changed-check-result']
            elif field == 'version':changed['terraform_version'] = 'another-version'
            else:changed['new-state-field'] = None
            with self.subTest(field=field):self.assertFalse(lifecycle.state_matches(self.state, changed))
        changed = copy.deepcopy(self.state);changed['outputs']['deployment']['value']['present'] = 1
        self.assertFalse(lifecycle.state_matches(self.state, changed))

    def test_missing_null_and_empty_fields_remain_distinct(self):
        absent = copy.deepcopy(self.state);del absent['check_results']
        null = {**absent, 'check_results': None};empty = {**absent, 'check_results': []}
        for left, right in ((absent, null), (null, empty), (absent, empty)):
            self.assertFalse(lifecycle.state_matches(left, right))
        for value in (absent, null, empty):self.assertTrue(lifecycle.state_matches(value, copy.deepcopy(value)))
        missing = copy.deepcopy(self.state);del missing['check_results'][0]['objects']
        null_objects = copy.deepcopy(missing);null_objects['check_results'][0]['objects'] = None
        empty_objects = copy.deepcopy(missing);empty_objects['check_results'][0]['objects'] = []
        for right in (null_objects, empty_objects):self.assertFalse(lifecycle.state_matches(missing, right))
        self.assertFalse(lifecycle.state_matches(null_objects, empty_objects))

    def test_malformed_or_duplicate_check_identities_are_rejected_even_if_both_operands_match(self):
        for kind in ('checks-type', 'config-missing', 'config-empty', 'config-type', 'config-duplicate',
                     'objects-type', 'object-missing', 'object-empty', 'object-type', 'object-duplicate'):
            invalid = copy.deepcopy(self.state)
            group = invalid['check_results'][0]
            if kind == 'checks-type':invalid['check_results'] = {}
            elif kind == 'config-missing':del group['config_addr']
            elif kind == 'config-empty':group['config_addr'] = ''
            elif kind == 'config-type':group['config_addr'] = 1
            elif kind == 'config-duplicate':invalid['check_results'].append(copy.deepcopy(group))
            elif kind == 'objects-type':group['objects'] = {}
            elif kind == 'object-missing':del group['objects'][0]['object_addr']
            elif kind == 'object-empty':group['objects'][0]['object_addr'] = ''
            elif kind == 'object-type':group['objects'][0]['object_addr'] = 1
            else:group['objects'].append(copy.deepcopy(group['objects'][0]))
            with self.subTest(kind=kind), self.assertRaises(HostError):
                lifecycle.state_matches(invalid, copy.deepcopy(invalid))


class ControllerRaceGuardTests(unittest.TestCase):
    @contextlib.contextmanager
    def case(self, kind):
        case = normal_tests.OnDemandTests() if kind == 'normal' else s3_tests.S3SessionTests()
        case.setUp()
        try:
            session = case.session(True) if kind == 'normal' else case.create(True)
            case.tofu.current['check_results'] = checks()
            yield case, session
        finally:case.doCleanups()

    def test_both_normal_and_s3_stop_accept_check_order_permutation_between_plan_and_apply(self):
        for kind in ('normal', 's3'):
            with self.subTest(kind=kind), self.case(kind) as (case, session):
                original = case.tofu.plan
                def plan(*args, **kwargs):
                    result = original(*args, **kwargs);reorder(case.tofu.current);return result
                case.tofu.plan = plan
                self.assertEqual(session.stop()['host'], 'absent')
                self.assertEqual(case.tofu.applies, 1)

    def test_both_race_guards_still_refuse_changed_values_before_any_apply(self):
        for kind in ('normal', 's3'):
            for change in ('serial', 'resource', 'output', 'status', 'message'):
                with self.subTest(kind=kind, change=change), self.case(kind) as (case, session):
                    original = case.tofu.plan
                    def plan(*args, **kwargs):
                        result = original(*args, **kwargs);reorder(case.tofu.current)
                        if change == 'serial':case.tofu.current['serial'] += 1
                        elif change == 'resource':case.tofu.current['resources'][0]['instances'][0]['attributes']['id'] = 'unreviewed-resource'
                        elif change == 'output':case.tofu.current['outputs'] = {'unreviewed': {'value': True}}
                        elif change == 'status':case.tofu.current['check_results'][0]['status'] = 'fail'
                        else:case.tofu.current['check_results'][0]['objects'][0]['failure_messages'] = ['changed-result']
                        return result
                    case.tofu.plan = plan
                    with self.assertRaises(HostError):session.stop()
                    self.assertEqual(case.tofu.applies, 0)
