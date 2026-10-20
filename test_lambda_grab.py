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
             mock.patch.object(lg, "find_instance_by_name", lambda *a, **k: None), \
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
    """A launch request that was or may have been accepted is never sent again."""

    def grab(self, waits=(), launch_errors=(), lookup=None, count=2, capacity=None):
        polls = iter(capacity or [caps(gpu_8x_b200_sxm6=["us-east-1"])] * 200)
        launches, waits, errs = [], list(waits), list(launch_errors)
        self.lookups = 0

        def launch(**kw):
            launches.append(kw["name"])
            if errs:
                e = errs.pop(0)
                if e:
                    raise e
            return [f"id-{kw['name']}"]

        def wait(ids, timeout=600):
            w = waits.pop(0) if waits else "active"
            if isinstance(w, Exception):
                raise w
            return [{"id": ids[0], "status": w, "ip": "1.2.3.4", "private_ip": "10.0.0.1"}]

        def find(name, *a, **k):
            self.lookups += 1
            iid = lookup(name, self.lookups) if lookup else None
            return {"id": iid, "instance_type": {"name": "gpu_8x_b200_sxm6"}, "region": {"name": "us-east-1"}} if iid else None
        clock = iter(range(0, 10**7, 10))   # fake clock, 10 s per call, so the reconcile window ends
        args = types.SimpleNamespace(instance_types=["gpu_8x_b200_sxm6"], count=count, ssh_key="k", extra_ssh_keys=[],
                                     ssh_key_path=None, region="us-east-1", region_only=True, poll_interval=0,
                                     name="b200-us-east-1", name_start=3, filesystems=[], each=False, dry_run=False)
        with mock.patch.object(lg, "list_instance_types", lambda: next(polls)), \
             mock.patch.object(lg, "launch_instances", launch), \
             mock.patch.object(lg, "wait_for_instances", wait), \
             mock.patch.object(lg, "find_instance_by_name", find), \
             mock.patch.object(lg, "notify", lambda m: None), \
             mock.patch.object(lg, "print_network_setup", lambda inst, name: ""), \
             mock.patch.object(lg.time, "time", lambda: next(clock)), \
             mock.patch.object(lg.time, "sleep", lambda s: None):
            lg.poll_and_launch(args)
        return launches

    def test_wait_timeout_does_not_relaunch(self):
        # reviewer's first case: first wait times out -> previously re-POSTed (3 VMs: 003, 003, 004)
        self.assertEqual(self.grab([TimeoutError("slow boot"), "active", "active"]), ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_status_errors_do_not_relaunch(self):
        self.assertEqual(self.grab([RuntimeError("HTTP 502 GET /instances/x"), "active", "active"]),
                         ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_failed_instance_not_replaced(self):
        self.assertEqual(self.grab([RuntimeError("One or more instances failed: ['error']"), "active"]),
                         ["b200-us-east-1-003", "b200-us-east-1-004"])

    def test_unknown_outcome_instance_appears_late(self):
        # reviewer's second case: lookup misses before the POST, POST times out, the instance shows up only on a later
        # lookup (eventual visibility) -> adopted, exactly one POST for 003
        def lookup(name, n):
            return "id-late" if name.endswith("-003") and n >= 4 else None
        self.assertEqual(self.grab(launch_errors=[TimeoutError("read timed out")], lookup=lookup, count=1),
                         ["b200-us-east-1-003"])

    def test_unknown_outcome_never_visible_stops(self):
        with self.assertRaises(SystemExit) as cm:   # one POST (502: outcome unknown), never visible -> stop, no retry
            self.grab(launch_errors=[lg.ApiHTTPError(502, "HTTP 502 POST launch")], count=1)
        self.assertEqual(cm.exception.code, 3)

    def test_definite_rejection_is_retried(self):
        self.assertEqual(self.grab(launch_errors=[lg.ApiHTTPError(400, "insufficient capacity")], count=1),
                         ["b200-us-east-1-003", "b200-us-east-1-003"])

    def test_existing_adopted_without_capacity(self):
        # 003 exists, no capacity anywhere for a while: 003 is adopted (lookup before any capacity check), 004 waits
        cap = [caps(gpu_8x_b200_sxm6=[])] * 3 + [caps(gpu_8x_b200_sxm6=["us-east-1"])] * 50
        launches = self.grab(lookup=lambda name, n: "id-003" if name.endswith("-003") else None, capacity=cap)
        self.assertEqual(launches, ["b200-us-east-1-004"])
        self.assertGreaterEqual(self.lookups, 2)


class AdoptionChecksTypeAndRegion(unittest.TestCase):
    """Name adoption must match the intended instance type and region (reviewer: --each A/B ordinal clash)."""

    def cloud(self, insts):
        return mock.patch.object(lg, "_request", lambda m, path, *a, **k: {"data": insts} if path == "/instances" else {})

    def inst(self, name, type_name, region, status="active"):
        return {"id": f"id-{name}", "name": name, "status": status, "instance_type": {"name": type_name}, "region": {"name": region}}

    def test_match_adopted(self):
        with self.cloud([self.inst("x-003", "A", "us-east-1")]):
            self.assertEqual(lg.find_instance_by_name("x-003", "A", "us-east-1")["id"], "id-x-003")

    def test_terminated_ignored(self):
        with self.cloud([self.inst("x-003", "A", "us-east-1", status="terminated")]):
            self.assertIsNone(lg.find_instance_by_name("x-003", "A", "us-east-1"))

    def test_type_clash_stops(self):
        with self.cloud([self.inst("x-003", "A", "us-east-1")]), mock.patch.object(lg, "notify", lambda m: None):
            with self.assertRaises(SystemExit) as cm:
                lg.find_instance_by_name("x-003", "B", "us-east-1")
        self.assertEqual(cm.exception.code, 4)

    def test_region_clash_stops(self):
        with self.cloud([self.inst("x-003", "A", "us-west-1")]), mock.patch.object(lg, "notify", lambda m: None):
            with self.assertRaises(SystemExit):
                lg.find_instance_by_name("x-003", "A", "us-east-1")

    def test_each_lost_reply_does_not_starve_other_type(self):
        # 1x A + 1x B; A's launch is accepted but its reply is lost. Expected: A adopted as 003, B launched as 004 (type B).
        cloud, posts = [], []

        def launch(**kw):
            posts.append((kw["instance_type"], kw["name"]))
            cloud.append(self.inst(kw["name"], kw["instance_type"], kw["region"], status="booting"))
            if kw["instance_type"] == "A" and len([p for p in posts if p[0] == "A"]) == 1:
                raise TimeoutError("reply lost")
            return [f"id-{kw['name']}"]
        clock = iter(range(0, 10**7, 10))
        args = types.SimpleNamespace(instance_types=["A", "B"], count=1, ssh_key="k", extra_ssh_keys=[], ssh_key_path=None,
                                     region="us-east-1", region_only=True, poll_interval=0, name="x", name_start=3,
                                     filesystems=[], each=True, dry_run=False)
        with self.cloud(cloud), \
             mock.patch.object(lg, "list_instance_types", lambda: caps(A=["us-east-1"], B=["us-east-1"])), \
             mock.patch.object(lg, "launch_instances", launch), \
             mock.patch.object(lg, "_report_launched", lambda *a, **k: None), \
             mock.patch.object(lg, "notify", lambda m: None), \
             mock.patch.object(lg.time, "time", lambda: next(clock)), \
             mock.patch.object(lg.time, "monotonic", lambda: next(clock)), \
             mock.patch.object(lg.time, "sleep", lambda s: None):
            lg.poll_each(args)
        self.assertEqual(posts, [("A", "x-003"), ("B", "x-004")])
        self.assertEqual(sorted((i["instance_type"]["name"], i["name"]) for i in cloud), [("A", "x-003"), ("B", "x-004")])


class PhaseZeroTypeSetAndLock(unittest.TestCase):
    """Reviewer: adopted instances must be one of the requested types, and the first instance fixes type+region."""

    def inst(self, name, type_name, region):
        return {"id": f"id-{name}", "name": name, "status": "active", "instance_type": {"name": type_name},
                "region": {"name": region}}

    def run_grab(self, cloud, wanted, region_only, capacity, count=2):
        posts = []

        def launch(**kw):
            posts.append((kw["instance_type"], kw["region"], kw["name"]))
            cloud.append(self.inst(kw["name"], kw["instance_type"], kw["region"]))
            return [f"id-{kw['name']}"]
        args = types.SimpleNamespace(instance_types=wanted, count=count, ssh_key="k", extra_ssh_keys=[], ssh_key_path=None,
                                     region="us-east-1", region_only=region_only, poll_interval=0, name="x", name_start=3,
                                     filesystems=[], each=False, dry_run=False)
        with mock.patch.object(lg, "_request", lambda m, path, *a, **k: {"data": cloud} if path == "/instances" else {}), \
             mock.patch.object(lg, "list_instance_types", lambda: capacity), \
             mock.patch.object(lg, "launch_instances", launch), \
             mock.patch.object(lg, "wait_for_instances", lambda ids, timeout=600: [{"id": ids[0], "status": "active"}]), \
             mock.patch.object(lg, "notify", lambda m: None), \
             mock.patch.object(lg, "print_network_setup", lambda inst, name: ""), \
             mock.patch.object(lg.time, "sleep", lambda s: None):
            lg.poll_and_launch(args)
        return posts

    def test_existing_of_unrequested_type_stops(self):
        cloud = [self.inst("x-003", "C", "us-east-1")]
        with self.assertRaises(SystemExit) as cm:
            self.run_grab(cloud, ["A", "B"], False, caps(A=["us-east-1"], B=["us-east-1"]))
        self.assertEqual(cm.exception.code, 4)
        self.assertEqual(len(cloud), 1)   # zero launches

    def test_adopted_first_instance_fixes_type_and_region(self):
        cloud = [self.inst("x-003", "A", "us-west-1")]
        posts = self.run_grab(cloud, ["A", "B"], False, caps(A=["us-east-1", "us-west-1"], B=["us-east-1"]))
        self.assertEqual(posts, [("A", "us-west-1", "x-004")])

    def test_adopted_second_instance_must_match_first(self):
        # 003 launched as A/us-east-1 by this run; 004 already exists but as B -> stop, no launch for 004
        cloud = [self.inst("x-004", "B", "us-east-1")]
        with self.assertRaises(SystemExit) as cm:
            self.run_grab(cloud, ["A", "B"], True, caps(A=["us-east-1"], B=["us-east-1"]))
        self.assertEqual(cm.exception.code, 4)
        self.assertEqual([i["name"] for i in cloud], ["x-004", "x-003"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
