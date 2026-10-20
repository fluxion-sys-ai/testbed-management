"""Offline tests for lambda_grab.py (no network): --region-only and --name-start. Run: python3 test_lambda_grab.py"""
import types
import unittest
from unittest import mock

import lambda_grab as lg


def caps(**regions_by_type):
    return {t: {"regions_with_capacity_available": [{"name": r} for r in rs]} for t, rs in regions_by_type.items()}


class FindCandidate(unittest.TestCase):
    def test_fallback_without_region_only(self):
        self.assertEqual(lg.find_candidate(caps(b8=["australia-east-1"]), ["b8"], "us-east-1"), ("b8", "australia-east-1"))

    def test_region_only_ignores_other_regions(self):
        self.assertIsNone(lg.find_candidate(caps(b8=["australia-east-1"]), ["b8"], "us-east-1", region_only=True))

    def test_region_only_takes_preferred(self):
        self.assertEqual(lg.find_candidate(caps(b8=["australia-east-1", "us-east-1"]), ["b8"], "us-east-1", True),
                         ("b8", "us-east-1"))


class Names(unittest.TestCase):
    def test_numbered(self):
        a = types.SimpleNamespace(name_start=3)
        self.assertEqual([lg.instance_name("b200-us-east-1", n, a) for n in (1, 2)], ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_default_unchanged(self):
        a = types.SimpleNamespace(name_start=None)
        self.assertEqual(lg.instance_name("exp", 2, a, suffix="-gpu_8x"), "exp-gpu_8x")


class GrabLoop(unittest.TestCase):
    """poll_and_launch with mocked API: capacity first only in another region, then in us-east-1.
    (Without --region-only a second instance would stay locked to the first region, so that case launches one.)"""

    def run_grab(self, region_only, count=2):
        polls = iter([caps(gpu_8x_b200_sxm6=["australia-east-1"])] + [caps(gpu_8x_b200_sxm6=["us-east-1"])] * 5)
        launches = []

        def launch(**kw):
            launches.append((kw["region"], kw["name"]))
            return [f"id{len(launches)}"]
        args = types.SimpleNamespace(instance_types=["gpu_8x_b200_sxm6"], count=count, ssh_key="k", extra_ssh_keys=[],
                                     ssh_key_path=None, region="us-east-1", region_only=region_only, poll_interval=0,
                                     name="b200-us-east-1", name_start=3, filesystems=[], each=False, dry_run=False)
        with mock.patch.object(lg, "list_instance_types", lambda: next(polls)), \
             mock.patch.object(lg, "launch_instances", launch), \
             mock.patch.object(lg, "find_instance_by_name", lambda n: None), \
             mock.patch.object(lg, "wait_for_instances", lambda ids, timeout=600: [{"id": i, "ip": "1.2.3.4", "private_ip": "10.0.0.1", "name": i} for i in ids]), \
             mock.patch.object(lg, "notify", lambda m: None), \
             mock.patch.object(lg, "print_network_setup", lambda inst, name: ""), \
             mock.patch.object(lg.time, "sleep", lambda s: None):
            lg.poll_and_launch(args)
        return launches

    def test_region_only_waits_for_us_east(self):
        self.assertEqual(self.run_grab(True), [("us-east-1", "b200-us-east-1-003"), ("us-east-1", "b200-us-east-1-004")])

    def test_without_region_only_falls_back(self):
        self.assertEqual(self.run_grab(False, count=1), [("australia-east-1", "b200-us-east-1-003")])


class NoOverLaunch(unittest.TestCase):
    """Once a launch request is accepted it is never repeated, whatever happens while the instance boots."""

    def grab(self, waits, launch_errors=(), existing=None, count=2):
        polls = iter([caps(gpu_8x_b200_sxm6=["us-east-1"])] * 50)
        launches, waits = [], list(waits)
        errs = list(launch_errors)

        def launch(**kw):
            if errs:
                e = errs.pop(0)
                if e:
                    raise e
            launches.append(kw["name"])
            return [f"id-{kw['name']}"]

        def wait(ids, timeout=600):
            w = waits.pop(0) if waits else "active"
            if isinstance(w, Exception):
                raise w
            return [{"id": ids[0], "status": w, "ip": "1.2.3.4", "private_ip": "10.0.0.1"}]
        args = types.SimpleNamespace(instance_types=["gpu_8x_b200_sxm6"], count=count, ssh_key="k", extra_ssh_keys=[],
                                     ssh_key_path=None, region="us-east-1", region_only=True, poll_interval=0,
                                     name="b200-us-east-1", name_start=3, filesystems=[], each=False, dry_run=False)
        with mock.patch.object(lg, "list_instance_types", lambda: next(polls)), \
             mock.patch.object(lg, "launch_instances", launch), \
             mock.patch.object(lg, "wait_for_instances", wait), \
             mock.patch.object(lg, "find_instance_by_name", lambda n: (existing or {}).get(n)), \
             mock.patch.object(lg, "notify", lambda m: None), \
             mock.patch.object(lg, "print_network_setup", lambda inst, name: ""), \
             mock.patch.object(lg.time, "sleep", lambda s: None):
            lg.poll_and_launch(args)
        return launches

    def test_wait_timeout_does_not_relaunch(self):
        # reviewer's case: first wait times out -> previously re-POSTed (3 VMs: 003, 003, 004)
        self.assertEqual(self.grab([TimeoutError("slow boot"), "active", "active"]), ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_status_errors_do_not_relaunch(self):
        self.assertEqual(self.grab([RuntimeError("HTTP 502 GET /instances/x"), "active", "active"]),
                         ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_failed_instance_not_replaced(self):
        self.assertEqual(self.grab([RuntimeError("One or more instances failed: ['error']"), "active"]),
                         ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_errored_launch_that_was_accepted_is_adopted(self):
        # POST raised (e.g. read timeout) but the instance exists under its name: adopt, do not POST again
        self.assertEqual(self.grab([], launch_errors=[OSError("read timed out")],
                                   existing={"b200-us-east-1-003": "id-adopted"}), ["b200-us-east-1-004"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
