from __future__ import annotations

import datetime as dt
import unittest

from test_provenance import DEPLOYMENT, JOURNAL, FakeAWS
from minecraft_host import HostError
from minecraft_provenance import WAIT_SECONDS, verify_fresh_volume


class FreshVolumeClockTests(unittest.TestCase):
    def test_wall_clock_jump_cannot_shorten_or_extend_the_observed_wait(self):
        for shift in (3600, -3600):
            with self.subTest(shift=shift):
                aws = FakeAWS()
                current = aws.utcnow
                aws.utcnow = lambda: current() + dt.timedelta(seconds=shift)
                started = aws.monotonic()
                proof = verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)
                self.assertGreaterEqual(aws.monotonic() - started, WAIT_SECONDS)
                self.assertGreaterEqual(proof['mandatory_wait_elapsed_seconds'], WAIT_SECONDS)
                checks = [when for service, _, _, when in aws.calls if service in ('cloudtrail', 'ssm')]
                self.assertTrue(checks)
                self.assertTrue(all(when - started >= WAIT_SECONDS for when in checks))

    def test_resumed_attempt_restarts_a_full_observed_wait(self):
        aws = FakeAWS()
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL, timeout=300)
        self.assertFalse(any(service in ('cloudtrail', 'ssm') for service, _, _, _ in aws.calls))
        resumed = aws.monotonic()
        proof = verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)
        self.assertGreaterEqual(aws.monotonic() - resumed, WAIT_SECONDS)
        self.assertGreaterEqual(proof['mandatory_wait_elapsed_seconds'], WAIT_SECONDS)

    def test_old_attachment_wall_age_does_not_credit_a_new_observation(self):
        aws = FakeAWS()
        aws.clock = 1800
        started = aws.monotonic()
        proof = verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)
        self.assertGreaterEqual(aws.monotonic() - started, WAIT_SECONDS)
        self.assertGreaterEqual(proof['mandatory_wait_elapsed_seconds'], WAIT_SECONDS)


if __name__ == '__main__':
    unittest.main()
