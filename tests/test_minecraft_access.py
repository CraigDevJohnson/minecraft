from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from minecraft_access import AccessError, change_allowlist, change_operators  # noqa: E402

JAVA_PLAYER = {"uuid": "12345678-1234-4234-8234-123456789abc", "name": "TestPlayer"}
BEDROCK_PLAYER = {"uuid": "00000000-0000-0000-0009-01eed05d1005", "name": "SampleTablet"}


class FakeMachine:
    """Private RCON/file boundary; no real player identities or server process."""

    def __init__(self, player=JAVA_PLAYER):
        self.player = player
        self.entries = []
        self.commands = []
        self.now = 0
        self.delay = 0
        self.requested = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def read_allowlist(self, instance):
        if self.requested is not None and self.now >= self.delay:
            self.entries = self.requested
        return list(self.entries)

    def rcon(self, instance, command):
        self.commands.append((instance, command))
        self.requested = [] if " remove " in command else [dict(self.player)]
        return "Added private identity to whitelist"


class AllowlistTests(unittest.TestCase):
    def test_java_add_uses_whitelist_and_confirms_uuid_without_disclosing_identity(self):
        machine = FakeMachine()
        result = change_allowlist(machine, "survival", "add", "java", JAVA_PLAYER)
        self.assertEqual(machine.commands, [("survival", "whitelist add TestPlayer")])
        self.assertEqual(result, {"instance": "survival", "allowlist": "updated", "edition": "java", "operation": "add"})

    def test_bedrock_uses_floodgate_without_prefix_and_waits_for_async_uuid_update(self):
        player = {**BEDROCK_PLAYER, "name": "家庭 Tablet"}
        machine = FakeMachine(player)
        machine.delay = 4
        result = change_allowlist(machine, "survival", "add", "bedrock", player)
        self.assertEqual(machine.commands, [("survival", 'fwhitelist add "家庭 Tablet"')])
        self.assertEqual(result["allowlist"], "updated")
        self.assertEqual(machine.now, 4)

    def test_remove_routes_by_edition_and_repeated_operations_do_not_send_commands(self):
        for edition, player, tool in (("java", JAVA_PLAYER, "whitelist"), ("bedrock", BEDROCK_PLAYER, "fwhitelist")):
            with self.subTest(edition=edition):
                machine = FakeMachine(player)
                machine.entries = [dict(player)]
                result = change_allowlist(machine, "survival", "add", edition, player)
                self.assertEqual(result["allowlist"], "unchanged")
                self.assertEqual(machine.commands, [])
                result = change_allowlist(machine, "survival", "remove", edition, player)
                self.assertEqual(result["allowlist"], "updated")
                self.assertEqual(machine.commands, [("survival", f"{tool} remove {player['name']}")])
                result = change_allowlist(machine, "survival", "remove", edition, player)
                self.assertEqual(result["allowlist"], "unchanged")
                self.assertEqual(len(machine.commands), 1)

    def test_rcon_or_file_failure_does_not_expose_private_identity(self):
        for boundary in ("rcon", "read_allowlist"):
            with self.subTest(boundary=boundary):
                machine = FakeMachine()
                def fail(*arguments):
                    raise RuntimeError("private identity and rcon-secret")
                setattr(machine, boundary, fail)
                with self.assertRaises(AccessError) as failure:
                    change_allowlist(machine, "survival", "add", "java", JAVA_PLAYER)
                self.assertNotIn("private identity", str(failure.exception))
                self.assertNotIn("rcon-secret", str(failure.exception))

    def test_corrupt_allowlist_cannot_be_treated_as_absent_or_valid_membership(self):
        for entries in (None, {}, [None], [{}], [{"uuid": "invalid"}], [JAVA_PLAYER, JAVA_PLAYER]):
            with self.subTest(entries=entries):
                machine = FakeMachine()
                machine.read_allowlist = lambda instance: entries
                with self.assertRaises(AccessError):
                    change_allowlist(machine, "survival", "remove", "java", JAVA_PLAYER)
                self.assertEqual(machine.commands, [])

    def test_command_injection_and_invalid_private_identities_fail_before_rcon(self):
        bad_players = [
            ("java", None),
            ("java", {}),
            ("java", {**JAVA_PLAYER, "uuid": "12345678123442348234123456789abc"}),
            ("java", {**JAVA_PLAYER, "uuid": JAVA_PLAYER["uuid"].upper()}),
            ("java", {**JAVA_PLAYER, "uuid": "00000000-0000-0000-0000-000000000000"}),
            ("java", {**JAVA_PLAYER, "name": "ab"}),
            ("java", {**JAVA_PLAYER, "name": "A" * 17}),
            ("java", {**JAVA_PLAYER, "name": "A B"}),
            ("java", {**JAVA_PLAYER, "name": "家庭Tablet"}),
            *[("bedrock", {**BEDROCK_PLAYER, "name": name}) for name in ("", " ", " Tablet", ".Tablet", "A" * 17, "Tablet;op Admin", 'Tablet"', "Tablet\\", "Tablet\nstop", "Tablet\x00", "Tablet\rstop", "Tablet\tName", "Tablet\u2028stop", "Tablet\u200b", "𐐀" * 9)],
        ]
        for edition, player in bad_players:
            with self.subTest(edition=edition, player=player):
                machine = FakeMachine()
                with self.assertRaises(AccessError):
                    change_allowlist(machine, "survival", "add", edition, player)
                self.assertEqual(machine.commands, [])

    def test_invalid_operation_edition_or_instance_cannot_disable_access_controls(self):
        for instance, operation, edition in (("survival", "off", "java"), ("survival", "reload", "java"), ("survival", "add", "offline"), ("../survival", "add", "java")):
            machine = FakeMachine()
            with self.assertRaises(AccessError):
                change_allowlist(machine, instance, operation, edition, JAVA_PLAYER)
            self.assertEqual(machine.commands, [])

    def test_unresolved_lookup_and_wrong_uuid_cannot_pass_on_rcon_success_text(self):
        for scenario in ("no-update", "wrong-uuid"):
            with self.subTest(scenario=scenario):
                machine = FakeMachine(BEDROCK_PLAYER)
                if scenario == "no-update":
                    machine.delay = 1000
                else:
                    machine.player = {**BEDROCK_PLAYER, "uuid": JAVA_PLAYER["uuid"]}
                with self.assertRaisesRegex(AccessError, "timed out"):
                    change_allowlist(machine, "survival", "add", "bedrock", BEDROCK_PLAYER)
                self.assertEqual(machine.now, 60)
                self.assertEqual(len(machine.commands), 1)

    def test_remove_waits_for_exact_uuid_to_disappear(self):
        machine = FakeMachine(BEDROCK_PLAYER)
        machine.entries = [dict(BEDROCK_PLAYER)]
        machine.delay = 3
        result = change_allowlist(machine, "survival", "remove", "bedrock", BEDROCK_PLAYER)
        self.assertEqual(result["allowlist"], "updated")
        self.assertEqual(machine.now, 3)

    def test_failed_remove_preserves_failure_until_membership_is_verified(self):
        machine = FakeMachine()
        machine.entries = [dict(JAVA_PLAYER)]
        machine.delay = 1000
        with self.assertRaisesRegex(AccessError, "timed out"):
            change_allowlist(machine, "survival", "remove", "java", JAVA_PLAYER)
        self.assertEqual(machine.now, 60)
        machine.now = 1000
        result = change_allowlist(machine, "survival", "remove", "java", JAVA_PLAYER)
        self.assertEqual(result["allowlist"], "unchanged")
        self.assertEqual(len(machine.commands), 1)


class FakeOperators(FakeMachine):
    """The same boundary, where op and deop change ops.json as the game does."""

    def read_operators(self, instance):
        return self.read_allowlist(instance)

    def rcon(self, instance, command):
        self.commands.append((instance, command))
        self.requested = [] if command.startswith("deop ") else [{**self.player, "level": 4, "bypassesPlayerLimit": False}]


class OperatorTests(unittest.TestCase):
    def test_java_op_names_the_account_and_confirms_the_uuid_in_ops_json(self):
        machine = FakeOperators()
        result = change_operators(machine, "survival", "add", "java", JAVA_PLAYER)
        self.assertEqual(machine.commands, [("survival", "op TestPlayer")])
        self.assertEqual(result, {"instance": "survival", "edition": "java", "operation": "add", "operators": "updated"})
        result = change_operators(machine, "survival", "add", "java", JAVA_PLAYER)
        self.assertEqual((result["operators"], len(machine.commands)), ("unchanged", 1))
        result = change_operators(machine, "survival", "remove", "java", JAVA_PLAYER)
        self.assertEqual((result["operators"], machine.commands[-1]), ("updated", ("survival", "deop TestPlayer")))

    def test_bedrock_goes_by_floodgates_name(self):
        player = {**BEDROCK_PLAYER, "name": "Sample Tablet"}
        machine = FakeOperators(player)
        machine.delay = 2
        result = change_operators(machine, "survival", "add", "bedrock", player)
        self.assertEqual(machine.commands, [("survival", "op .Sample_Tablet")])
        self.assertEqual((result["operators"], machine.now), ("updated", 2))

    def test_a_change_the_server_never_confirms_times_out_without_naming_the_player(self):
        machine = FakeOperators(BEDROCK_PLAYER)
        machine.delay = 1000
        with self.assertRaisesRegex(AccessError, "timed out; a player this world has not seen becomes an operator at its next start") as failure:
            change_operators(machine, "survival", "add", "bedrock", BEDROCK_PLAYER)
        self.assertNotIn("SampleTablet", str(failure.exception))
        self.assertLessEqual(len(str(failure.exception)), 200)  # the launcher passes on at most 200 bytes

    def test_corrupt_ops_or_invalid_input_fail_before_rcon(self):
        for entries in (None, {}, [None], [{"uuid": "invalid"}], [JAVA_PLAYER, JAVA_PLAYER]):
            with self.subTest(entries=entries):
                machine = FakeOperators()
                machine.read_operators = lambda instance: entries
                with self.assertRaises(AccessError):
                    change_operators(machine, "survival", "add", "java", JAVA_PLAYER)
                self.assertEqual(machine.commands, [])
        for instance, operation, edition, player in (
            ("survival", "op", "java", JAVA_PLAYER),
            ("survival", "add", "offline", JAVA_PLAYER),
            ("../survival", "add", "java", JAVA_PLAYER),
            ("survival", "add", "java", {**JAVA_PLAYER, "name": "A B"}),
            ("survival", "add", "bedrock", {**BEDROCK_PLAYER, "name": "Tablet;stop"}),
            ("survival", "add", "java", {"name": "TestPlayer"}),
        ):
            with self.subTest(instance=instance, operation=operation, edition=edition, player=player):
                machine = FakeOperators()
                with self.assertRaises(AccessError):
                    change_operators(machine, instance, operation, edition, player)
                self.assertEqual(machine.commands, [])

    def test_an_unreadable_ops_json_does_not_expose_its_cause(self):
        machine = FakeOperators()

        def fail(instance):
            raise RuntimeError("private identity")

        machine.read_operators = fail
        with self.assertRaises(AccessError) as failure:
            change_operators(machine, "survival", "add", "java", JAVA_PLAYER)
        self.assertEqual(str(failure.exception), "cannot read private server operators")


if __name__ == "__main__":
    unittest.main()
