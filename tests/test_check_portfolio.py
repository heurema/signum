import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_portfolio import run


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def sha256(content):
    return hashlib.sha256(content.encode()).hexdigest()


class PortfolioCheckTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.registry = self.root / "portfolio.json"
        self.review = self.root / "review.json"
        self.resources = [{"id": "evidence", "path": "shared/evidence"}]
        self.projects = [self.project("alpha", "owner-a"), self.project("beta", "owner-b")]
        self.save()

    def put(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def project(self, project_id, owner):
        root = f"projects/{project_id}"
        records = {}
        for role in ("decision", "acceptance"):
            content = f"{project_id} historical {role}"
            path = f"{role}.txt"
            self.put(f"{root}/{path}", content)
            records[role] = {"id": f"{role}@v1", "path": path, "sha256": sha256(content),
                             "scope": f"Own {project_id} {role}"}
        details = {"owner": owner, "environment": f"{project_id} local development", **records}
        content = f"{project_id} verified artifact"
        self.put(f"{root}/recovery.txt", content)
        self.put(f"shared/evidence/{project_id}.txt", content)
        project = {"id": project_id, "root": root, **details,
                   "artifact": {"resource": "evidence", "path": f"{project_id}.txt", "sha256": sha256(content),
                                "source": "recovery.txt"}}
        self.enroll(project)
        return project

    def enroll(self, project):
        artifact = project["artifact"]
        identity = {
            "project_id": project["id"],
            **{field: project[field] for field in ("owner", "environment", "decision", "acceptance")},
            "evidence": {"source": artifact["source"], "sha256": artifact["sha256"]},
        }
        write_json(self.root / project["root"] / ".portfolio-identity.json", identity)

    def save(self):
        write_json(self.registry, {"version": 1, "projects": self.projects, "resources": self.resources})

    def plan(self, claims=None):
        if claims is not None:
            write_json(self.review, claims)
        result, code = run(self.registry, self.review if claims is not None else None)
        self.assertEqual(code, 0, result)
        return result

    def fault(self):
        (self.root / "shared/evidence").rename(self.root / "shared/detached")

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_healthy_to_common_fault_is_single_stable_read_only_repair(self):
        healthy = self.plan()
        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(healthy["proposals"], [])
        self.fault()
        before = self.snapshot()
        damaged = self.plan()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(damaged["status"], "needs_repair")
        self.assertEqual(len(damaged["proposals"]), 1)
        proposal, = damaged["proposals"]
        self.assertEqual(proposal["kind"], "restore_resource")
        self.assertEqual(proposal["target"], "shared/evidence")
        self.assertEqual([scope["project_id"] for scope in proposal["required_scopes"]], ["alpha", "beta"])
        self.assertEqual([write["sha256"] for write in proposal["writes"]],
                         [project["artifact"]["sha256"] for project in self.projects])
        for state in damaged["projects"]:
            self.assertEqual(state["decision"]["status"], "verified")
            self.assertEqual(state["acceptance"]["status"], "verified")
            self.assertEqual(state["artifact"]["recovery"]["status"], "verified")
        self.resources.insert(0, {"id": "unrelated", "path": "other"})
        self.projects.reverse()
        self.save()
        first = self.plan()
        self.resources.reverse()
        self.save()
        self.assertEqual(first, self.plan())
        self.assertEqual(first["proposals"], damaged["proposals"])
        (self.root / "shared/detached").rename(self.root / "shared/evidence")
        self.assertEqual(self.plan()["status"], "healthy")

    def test_review_claims_require_each_projects_current_scope(self):
        self.fault()
        original = self.plan()
        proposal_id = original["proposals"][0]["id"]
        review = {"proposal_id": proposal_id, "project_id": "alpha", "owner": "owner-a",
                  "decision_id": "decision@v1", "acceptance_id": "acceptance@v1", "disposition": "approve"}
        foreign = {**review, "project_id": "beta"}
        stale = {**review, "proposal_id": "stale"}
        wrong = {**review, "acceptance_id": "acceptance@v0"}
        checked = self.plan([review, foreign, stale, wrong])
        self.assertEqual([row["status"] for row in checked["queue"]], ["approve_claimed", "pending"])
        self.assertEqual(checked["proposals"][0]["status"], "pending")
        self.assertEqual(len(checked["review_issues"]), 3)
        before = self.snapshot()
        cli = subprocess.run([sys.executable, "-B", str(ROOT / "tools/check_portfolio.py"),
                              "--registry", str(self.registry), "--review", str(self.review)],
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout), checked)
        self.assertEqual(self.snapshot(), before)
        beta_review = {**foreign, "owner": "owner-b"}
        checked = self.plan([review, beta_review])
        self.assertEqual(checked["proposals"][0]["status"], "all_approvals_claimed")
        self.assertEqual(checked["projects"], original["projects"])
        contradictory = self.plan([review, {**review, "disposition": "reject"}, beta_review])
        self.assertEqual(contradictory["queue"][0]["status"], "pending")

    def test_isolated_damage_ignores_other_project(self):
        (self.root / "projects/beta/decision.txt").unlink()
        (self.root / "shared/evidence/alpha.txt").unlink()
        result = self.plan()
        proposal, = result["proposals"]
        self.assertEqual([scope["project_id"] for scope in proposal["required_scopes"]], ["alpha"])
        self.assertEqual([state["status"] for state in result["projects"]], ["needs_repair", "blocked"])
        self.assertEqual(result["projects"][1]["blockers"], ["decision_unverified"])

    def test_shared_repair_blocks_if_enrollment_or_recovery_disagrees(self):
        self.fault()
        beta = self.projects[1]
        beta["acceptance"]["scope"] = "Relabeled scope"
        self.save()
        results = self.plan()
        self.assertEqual(results["status"], "blocked")
        self.assertEqual(results["proposals"], [])
        self.assertEqual(results["projects"][1]["identity"]["status"], "identity_mismatch")
        self.assertEqual(results["projects"][1]["acceptance"]["status"], "unbound_to_project")
        self.assertEqual(results["projects"][1]["acceptance"]["bytes_status"], "verified")
        beta["acceptance"]["scope"] = "Own beta acceptance"
        self.save()
        self.put("projects/beta/recovery.txt", "changed recovery bytes")
        results = self.plan()
        self.assertEqual(results["proposals"], [])
        self.assertIn("recovery_source_unverified", results["projects"][1]["blockers"])
        self.assertEqual(results["blockers"][0]["projects"], ["alpha", "beta"])

    def test_cross_resource_collision_blocks_healthy_consumer_and_conflicting_write(self):
        self.resources.append({"id": "parent", "path": "shared"})
        beta = self.projects[1]
        beta["artifact"].update(resource="parent", path="evidence/alpha.txt")
        self.save()
        result = self.plan()
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["blockers"][0]["reason"], "conflicting_target_digests")
        alpha, beta = result["projects"]
        self.assertEqual(alpha["artifact"]["status"], "verified")
        self.assertEqual(beta["artifact"]["status"], "digest_mismatch")
        self.assertEqual([alpha["status"], beta["status"]], ["blocked", "blocked"])
        self.assertEqual(result["blockers"][0]["targets"], ["shared/evidence/alpha.txt"])

    def test_registry_cannot_substitute_a_different_recovery_file_and_digest(self):
        self.fault()
        alpha = self.projects[0]
        self.put("projects/alpha/unrelated.txt", "unrelated bytes")
        alpha["artifact"].update(source="unrelated.txt", sha256=sha256("unrelated bytes"))
        self.save()
        result = self.plan()
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["projects"][0]["artifact"]["recovery"]["status"], "verified")
        self.assertEqual(result["projects"][0]["identity"]["status"], "identity_mismatch")

    def test_compatible_cross_resource_claims_coalesce(self):
        self.resources.append({"id": "parent", "path": "shared"})
        alpha, beta = self.projects
        beta["artifact"].update(resource="parent", path="evidence/alpha.txt", sha256=alpha["artifact"]["sha256"])
        self.put("projects/beta/recovery.txt", "alpha verified artifact")
        self.enroll(beta)
        self.save()
        self.fault()
        result = self.plan()
        proposal, = result["proposals"]
        self.assertEqual([scope["project_id"] for scope in proposal["required_scopes"]], ["alpha", "beta"])
        write, = proposal["writes"]
        self.assertEqual(write["target"], "shared/evidence/alpha.txt")
        self.assertEqual([source["project_id"] for source in write["recovery_sources"]], ["alpha", "beta"])

    def test_paths_and_aliases_fail_closed(self):
        self.projects[1]["root"] = self.projects[0]["root"]
        self.save()
        result, code = run(self.registry)
        self.assertEqual(code, 2)
        self.assertEqual(result["proposals"], [])
        self.projects[1]["root"] = "projects/beta"
        self.projects[1]["artifact"]["path"] = "../outside.txt"
        self.save()
        result, code = run(self.registry)
        self.assertEqual(code, 2)
        self.assertEqual(result["proposals"], [])
        self.projects[1]["artifact"]["path"] = "beta.txt"
        self.save()
        self.fault()
        (self.root / "shared/evidence").symlink_to(self.root / "shared/detached", target_is_directory=True)
        result = self.plan()
        self.assertEqual(result["proposals"], [])
        self.assertEqual([state["artifact"]["status"] for state in result["projects"]], ["unknown", "unknown"])


if __name__ == "__main__":
    unittest.main()
