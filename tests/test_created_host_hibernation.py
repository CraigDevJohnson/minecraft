import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import minecraft_on_demand as lifecycle
import minecraft_s3_session as s3
from minecraft_host import HostError
from test_on_demand_host import DEPLOYMENT, HOST_ATTRIBUTES, ATTACHMENT_ATTRIBUTES, state
from test_s3_session import s3state

HOST = 'aws_instance.host[0]'


class CreatedHostHibernationTests(unittest.TestCase):
    def change(self, value=None):
        return {'actions': ['create'], 'after': {**copy.deepcopy(HOST_ATTRIBUTES), 'hibernation': value}, 'after_unknown': {}}

    def test_recorded_omitted_hibernation_matches_only_default_disabled_host(self):
        change = self.change();original = copy.deepcopy(change)
        actual = {**copy.deepcopy(HOST_ATTRIBUTES), 'hibernation': False}
        self.assertFalse(lifecycle.matches_known(change['after'], actual, change['after_unknown']))
        self.assertTrue(lifecycle.created_resource_matches(HOST, change, actual))
        self.assertEqual(change, original)
        self.assertTrue(lifecycle.created_resource_matches(HOST, self.change(False), actual))

    def test_enabled_or_nonboolean_hibernation_is_refused_even_if_marked_unknown(self):
        for planned, observed in ((None, True), (True, True), (True, False), (False, True), (0, False), (False, 0)):
            for unknown in ({}, {'hibernation': True}):
                change = self.change(planned);change['after_unknown'] = unknown
                with self.subTest(planned=planned, observed=observed, unknown=bool(unknown)):
                    self.assertFalse(lifecycle.created_resource_matches(HOST, change, {**HOST_ATTRIBUTES, 'hibernation': observed}))

    def test_no_null_to_false_alias_for_other_resources_or_fields(self):
        actual = {**HOST_ATTRIBUTES, 'hibernation': False}
        for address in ('aws_instance.other[0]', 'aws_ebs_volume.session_data[0]', 'aws_volume_attachment.data[0]'):
            with self.subTest(address=address):self.assertFalse(lifecycle.created_resource_matches(address, self.change(), actual))
        change = self.change();change['after']['disable_api_stop'] = None
        self.assertFalse(lifecycle.created_resource_matches(HOST, change, {**actual, 'disable_api_stop': False}))

    def test_all_other_known_configuration_and_unknown_masks_keep_their_existing_semantics(self):
        actual = {**copy.deepcopy(HOST_ATTRIBUTES), 'hibernation': False}
        for field, value in (('ami', 'another-image'), ('subnet_id', 'another-subnet'), ('instance_type', 'another-size')):
            bad = {**actual, field: value}
            with self.subTest(field=field):self.assertFalse(lifecycle.created_resource_matches(HOST, self.change(), bad))
        bad = copy.deepcopy(actual);bad['root_block_device'][0]['delete_on_termination'] = False
        self.assertFalse(lifecycle.created_resource_matches(HOST, self.change(), bad))
        change = self.change();change['after']['id'] = None;change['after_unknown']['id'] = True
        self.assertTrue(lifecycle.created_resource_matches(HOST, change, actual))

    def test_normal_created_verification_accepts_disabled_default_and_refuses_enabled_actual(self):
        current = state(True)
        host = lifecycle.resources(current)[HOST];host['hibernation'] = False
        journal = {'initial_state': state(False), 'planned_changes': [{'address': HOST, 'change': self.change()}]}
        lifecycle.verify_resumed_sessions(journal, current)
        host['hibernation'] = True
        with self.assertRaises(HostError):lifecycle.verify_resumed_sessions(journal, current)

    def test_s3_created_verification_uses_the_same_contextual_matcher(self):
        current = s3state(True)
        host = lifecycle.resources(current)[HOST];host['hibernation'] = False
        journal = {'initial_state': s3state(False), 'planned_changes': [{'address': HOST, 'change': self.change()}]}
        s3.S3Session.verify_recorded_sessions(None, journal, current)
        host['hibernation'] = True
        with self.assertRaises(HostError):s3.S3Session.verify_recorded_sessions(None, journal, current)

    def test_legacy_and_explicit_disabled_create_plans_pass_and_enabled_create_is_rejected(self):
        for value in (None, False, True, 0):
            plan = {'resource_changes': [{'address': HOST, 'change': self.change(value)},
                    {'address': 'aws_volume_attachment.data[0]', 'change': {'actions': ['create'], 'after': ATTACHMENT_ATTRIBUTES}}]}
            with self.subTest(value=value):
                if value is None or value is False:lifecycle.validate_plan(plan, present=True, state=state(False), deployment=DEPLOYMENT)
                else:
                    with self.assertRaises(HostError):lifecycle.validate_plan(plan, present=True, state=state(False), deployment=DEPLOYMENT)
        current = state(True)
        plan = {'resource_changes': [{'address': key, 'change': {'actions': ['delete'], 'before': value}}
                                    for key, value in lifecycle.resources(current).items() if key in lifecycle.SESSION_ADDRESSES]}
        lifecycle.validate_plan(plan, present=False, state=current, deployment=DEPLOYMENT)
