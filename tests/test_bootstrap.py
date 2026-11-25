import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPO_ID, OWNER_ID, NUMBER = "111", "222", "333"
PLAN = "terraform-plan@demo.iam.gserviceaccount.com"
APPLY = "terraform-apply@demo.iam.gserviceaccount.com"
PRINCIPALS = f"principalSet://iam.googleapis.com/projects/{NUMBER}/locations/global"
PLAN_MEMBER = (
    f"{PRINCIPALS}/workloadIdentityPools/github/attribute.event_actor/pull_request:{OWNER_ID}"
)
APPLY_MEMBER = (
    f"{PRINCIPALS}/workloadIdentityPools/github-production/attribute.environment/production"
)
FROM_REPO = f'assertion.repository_id=="{REPO_ID}" && assertion.repository_owner_id=="{OWNER_ID}"'
MAPPING = (
    "google.subject=assertion.sub,attribute.workflow_ref=assertion.workflow_ref,"
    'attribute.event_actor=assertion.event_name+":"+assertion.actor_id'
)
PRODUCTION_MAPPING = "google.subject=assertion.sub,attribute.environment=assertion.environment"
PRODUCTION_CONDITION = (
    f'{FROM_REPO} && assertion.environment=="production" && assertion.ref=="refs/heads/main"'
)

STUB = """
import json, os, sys

tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
config = json.load(open(os.environ["STUB_CONFIG"]))
stdin = sys.stdin.read() if "--input" in args else None
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([tool, *args, stdin] if stdin else [tool, *args]) + "\\n")
line = " ".join(args)
exists = config["exists"]
if tool == "gh":
    if args[:2] == ["repo", "view"]:
        print("owner/repo")
    elif line.startswith("api repos/owner/repo --jq"):
        print(f"111 222 {config['visibility']}")
    elif line.startswith("api user"):
        print("222")
    elif line.startswith("api repos/owner/repo/environments/production --jq"):
        print("\\n".join(config["rules"]))
    elif line.startswith("api repos/owner/repo/environments/production/deployment"):
        print("main" if exists else "")
elif args[:2] == ["projects", "describe"]:
    print("333")
elif args[:2] == ["config", "get-value"]:
    print("owner-account")
elif "describe" in args and "billing" not in args:
    sys.exit(0 if exists else 1)
elif "get-iam-policy" in args:
    print("\\n".join(config["members"].get(args[3], [])))
elif args[:3] == ["billing", "projects", "describe"]:
    print("billingAccounts/stub-billing")
elif args[:3] == ["billing", "budgets", "list"]:
    print("billingAccounts/stub-billing/budgets/1" if exists else "")
"""


def run(
    tmp_path: Path,
    project: str | None = "demo",
    exists: bool = False,
    visibility: str = "public",
    rules: tuple[str, ...] = ("required_reviewers", "branch_policy"),
    members: dict[str, list[str]] | None = None,
    dotenv: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run a copy of the script, away from the repo's .env, with stub gcloud and gh."""
    repo, bin_dir = tmp_path / "repo", tmp_path / "bin"
    (repo / "scripts").mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy(ROOT / "scripts" / "bootstrap.sh", repo / "scripts")
    (repo / "Makefile").write_text("")
    if dotenv is not None:
        (repo / ".env").write_text(dotenv)
    for tool in ("gcloud", "gh"):
        (bin_dir / tool).write_text(f"#!{sys.executable}\n{STUB}")
        (bin_dir / tool).chmod(0o755)
    if members is None:
        members = {PLAN: [PLAN_MEMBER], APPLY: [APPLY_MEMBER]} if exists else {}
    config: dict[str, Any] = {
        "exists": exists,
        "visibility": visibility,
        "rules": list(rules),
        "members": members,
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    log = tmp_path / "log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_CONFIG": str(tmp_path / "config.json"),
        "STUB_LOG": str(log),
    }
    if project is not None:
        env["GCP_PROJECT"] = project
    result = subprocess.run(
        ["sh", str(repo / "scripts" / "bootstrap.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def find(calls: list[list[str]], *words: str) -> list[list[str]]:
    return [call for call in calls if all(word in call for word in words)]


def flag(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


def test_first_run_sets_the_gate_before_any_identity(tmp_path: Path) -> None:
    result, calls = run(tmp_path)
    assert result.returncode == 0, result.stderr
    first_iam = next(i for i, c in enumerate(calls) if c[:2] == ["gcloud", "iam"])
    environment = [i for i, c in enumerate(calls) if any("environments/" in a for a in c)]
    assert len(environment) == 4
    assert max(environment) < first_iam
    put = find(calls, "PUT")[0]
    body = json.loads(put[-1])
    assert body["reviewers"] == [{"type": "User", "id": int(OWNER_ID)}]
    assert body["can_admins_bypass"] is False
    assert find(calls, "POST")[0][-4:] == ["-f", "name=main", "-f", "type=branch"]


def test_first_run_creates_two_pools_and_their_providers(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    pools = find(calls, "workload-identity-pools", "create")
    assert [c[4] for c in pools] == ["github", "github-production"]
    providers = find(calls, "create-oidc")
    assert [(c[5], flag(c, "--workload-identity-pool")) for c in providers] == [
        ("github", "github"),
        ("github", "github-production"),
    ]
    assert flag(providers[0], "--attribute-mapping") == MAPPING
    assert flag(providers[0], "--attribute-condition") == FROM_REPO
    assert flag(providers[1], "--attribute-mapping") == PRODUCTION_MAPPING
    assert flag(providers[1], "--attribute-condition") == PRODUCTION_CONDITION
    for call in providers:
        assert flag(call, "--issuer-uri") == "https://token.actions.githubusercontent.com"


def test_first_run_binds_each_identity_to_one_principal(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    bindings = find(calls, "add-iam-policy-binding", "roles/iam.workloadIdentityUser")
    assert [(c[4], flag(c, "--member")) for c in bindings] == [
        (PLAN, PLAN_MEMBER),
        (APPLY, APPLY_MEMBER),
    ]
    bucket = find(calls, "buckets", "add-iam-policy-binding")
    assert [flag(c, "--role") for c in bucket] == [
        "roles/storage.objectViewer",
        "roles/storage.objectAdmin",
    ]


def test_first_run_sets_the_secrets(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    secrets = {c[3]: flag(c, "--body") for c in find(calls, "secret", "set")}
    providers = f"projects/{NUMBER}/locations/global/workloadIdentityPools"
    assert secrets == {
        "GCP_PROJECT_ID": "demo",
        "GCP_PROJECT_NUMBER": NUMBER,
        "GCP_WIF_PROVIDER": f"{providers}/github/providers/github",
        "GCP_WIF_PROVIDER_PRODUCTION": f"{providers}/github-production/providers/github",
        "TF_STATE_BUCKET": "demo-terraform-state",
        "OWNER_EMAIL": "owner-account",
    }


def test_budget_is_last_and_hides_the_billing_account(tmp_path: Path) -> None:
    result, calls = run(tmp_path)
    assert calls[-1][:4] == ["gcloud", "billing", "budgets", "create"]
    assert flag(calls[-1], "--budget-amount") == "75"
    for call in find(calls, "budgets"):
        assert flag(call, "--billing-project") == "demo"
    assert "stub-billing" not in result.stdout + result.stderr


def test_second_run_updates_and_creates_nothing(tmp_path: Path) -> None:
    result, calls = run(tmp_path, exists=True)
    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if any(a in ("create", "create-oidc", "POST") for a in c)]
    updates = find(calls, "update-oidc")
    assert [
        (
            flag(c, "--workload-identity-pool"),
            flag(c, "--attribute-mapping"),
            flag(c, "--attribute-condition"),
        )
        for c in updates
    ] == [
        ("github", MAPPING, FROM_REPO),
        ("github-production", PRODUCTION_MAPPING, PRODUCTION_CONDITION),
    ]


def test_missing_project_prints_usage(tmp_path: Path) -> None:
    result, calls = run(tmp_path, project=None)
    assert result.returncode == 2
    assert "usage" in result.stderr
    assert calls == []


def test_project_can_come_from_dotenv(tmp_path: Path) -> None:
    result, calls = run(tmp_path, project=None, dotenv="GCP_PROJECT=demo\nBUDGET=20\n")
    assert result.returncode == 0, result.stderr
    assert flag(calls[-1], "--budget-amount") == "20"


def test_private_repo_stops_before_any_change(tmp_path: Path) -> None:
    result, calls = run(tmp_path, visibility="private")
    assert result.returncode == 1
    assert "public" in result.stderr
    assert [c for c in calls if c[0] == "gcloud" or "-X" in c] == []


def test_environment_without_reviewer_stops_before_google(tmp_path: Path) -> None:
    result, calls = run(tmp_path, rules=("branch_policy",))
    assert result.returncode == 1
    assert "reviewer" in result.stderr
    assert [c for c in calls if c[:2] == ["gcloud", "iam"]] == []


def test_unexpected_member_stops_the_run(tmp_path: Path) -> None:
    other = "principal://example/other"
    result, calls = run(tmp_path, exists=True, members={PLAN: [PLAN_MEMBER, other]})
    assert result.returncode == 1
    assert other in result.stderr
    assert find(calls, "secret") == []
