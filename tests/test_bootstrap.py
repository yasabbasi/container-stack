import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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
ISSUER = "https://token.actions.githubusercontent.com"
FROM_REPO = f'assertion.repository_id=="{REPO_ID}" && assertion.repository_owner_id=="{OWNER_ID}"'
MAPPING = (
    "google.subject=assertion.sub,attribute.workflow_ref=assertion.workflow_ref,"
    'attribute.event_actor=assertion.event_name+":"+assertion.actor_id'
)
PRODUCTION_MAPPING = "google.subject=assertion.sub,attribute.environment=assertion.environment"
PRODUCTION_CONDITION = (
    f'{FROM_REPO} && assertion.repository_visibility=="public"'
    ' && assertion.environment=="production" && assertion.ref=="refs/heads/main"'
)
POOL_FLAGS = ["--project", "demo", "--location", "global"]

STUB = """
import json, os, sys

tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
config = json.load(open(os.environ["STUB_CONFIG"]))
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}
stdin = sys.stdin.read() if "--input" in args else None
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([tool, *args, stdin] if stdin else [tool, *args]) + "\\n")
line = " ".join(args)


def save():
    json.dump(state, open(state_path, "w"))


def flag(name):
    return args[args.index(name) + 1]


def key():
    if "--workload-identity-pool" in args:
        return "provider/" + flag("--workload-identity-pool")
    if "workload-identity-pools" in args:
        return "pool/" + args[3]
    if "service-accounts" in args:
        return "account/" + args[3].split("@")[0]
    return "/".join(args[:2])


if any(word in line for word in config["fail"]):
    sys.exit(1)
for word, times in config["flaky"].items():
    if word in line and state.get("flaky/" + word, 0) < times:
        state["flaky/" + word] = state.get("flaky/" + word, 0) + 1
        save()
        sys.exit(1)
out = []
if tool == "sleep":
    pass
elif tool == "gh":
    if args[:2] == ["repo", "view"]:
        out = ["owner/repo"]
    elif line.startswith("api repos/owner/repo --jq"):
        out = [f"111 222 {config['visibility']}"]
    elif line.startswith("api user"):
        out = [config["user"]]
    elif line.startswith("api -X POST"):
        state["branches"] = state.get("branches", []) + ["main"]
    elif line.startswith("api repos/owner/repo/environments/production/"):
        out = state.get("branches", config["branches"])
    elif line.startswith("api repos/owner/repo/environments/production"):
        out = [config["gate"]]
elif args[:2] == ["projects", "describe"]:
    out = ["333"]
elif args[:2] == ["config", "get-value"]:
    out = [config["account"]]
elif args[:3] == ["billing", "projects", "describe"]:
    out = [config["billing"]]
elif args[:3] == ["billing", "budgets", "list"]:
    out = state.get("budget", [])
elif args[:3] == ["billing", "budgets", "create"]:
    state["budget"] = ["billingAccounts/stub-billing/budgets/1"]
elif "keys" in args:
    out = config["keys"].get(flag("--iam-account"), [])
elif "get-iam-policy" in args:
    members = state.get("member/" + args[3], []) + config["members"].get(args[3], [])
    out = ["roles/iam.workloadIdentityUser\\t" + member for member in members]
    out += config["rows"].get(args[3], [])
    # With no bindings, the real CLI prints one empty row: a lone tab.
    out = out or ["\\t"]
elif args[:2] == ["iam", "service-accounts"] and "add-iam-policy-binding" in args:
    members = state.setdefault("member/" + args[3], [])
    if flag("--member") not in members:
        members.append(flag("--member"))
elif "providers" in args and "list" in args:
    pool = flag("--workload-identity-pool")
    out = (["github"] if key() in state else []) + config["providers"].get(pool, [])
elif "describe" in args:
    sys.exit(0 if key() in state else 1)
elif "create" in args or "create-oidc" in args:
    state[key()] = True
save()
if out:
    print("\\n".join(out))
"""


def run(
    tmp_path: Path,
    project: str | None = "demo",
    dotenv: str | None = None,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    makefile: bool = True,
    **overrides: Any,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run a copy of the script, away from the repo's .env, with stub gcloud, gh and sleep."""
    repo, bin_dir = tmp_path / "repo", tmp_path / "bin"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    shutil.copy(ROOT / "scripts" / "bootstrap.sh", repo / "scripts")
    if makefile:
        (repo / "Makefile").write_text("")
    if dotenv is not None:
        (repo / ".env").write_text(dotenv)
    for tool in ("gcloud", "gh", "sleep"):
        (bin_dir / tool).write_text(f"#!{sys.executable}\n{STUB}")
        (bin_dir / tool).chmod(0o755)
    config: dict[str, Any] = {
        "visibility": "public",
        "user": OWNER_ID,
        "gate": f"false {OWNER_ID}",
        "branches": [],
        "account": "owner-account",
        "billing": "billingAccounts/stub-billing",
        "keys": {},
        "members": {},
        "rows": {},
        "providers": {},
        "fail": [],
        "flaky": {},
    } | overrides
    (tmp_path / "config.json").write_text(json.dumps(config))
    log = tmp_path / "log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_CONFIG": str(tmp_path / "config.json"),
        "STUB_STATE": str(tmp_path / "state.json"),
        "STUB_LOG": str(log),
    }
    if project is not None:
        env["GCP_PROJECT"] = project
    env |= env_extra or {}
    result = subprocess.run(
        ["sh", str(repo / "scripts" / "bootstrap.sh") if cwd else "scripts/bootstrap.sh"],
        cwd=cwd or repo,
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


def bindings(calls: list[list[str]]) -> list[list[str]]:
    return find(calls, "add-iam-policy-binding")


def test_first_run_sets_the_gate_before_any_identity(tmp_path: Path) -> None:
    result, calls = run(tmp_path)
    assert result.returncode == 0, result.stderr
    first_iam = next(i for i, c in enumerate(calls) if c[:2] == ["gcloud", "iam"])
    environment = [i for i, c in enumerate(calls) if any("environments/" in a for a in c)]
    assert len(environment) == 5
    assert max(environment) < first_iam
    body = json.loads(find(calls, "PUT")[0][-1])
    assert body["reviewers"] == [{"type": "User", "id": int(OWNER_ID)}]
    assert body["can_admins_bypass"] is False
    assert find(calls, "POST")[0][-4:] == ["-f", "name=main", "-f", "type=branch"]


def test_first_run_creates_two_pools_and_their_providers(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    pools = find(calls, "workload-identity-pools", "create")
    assert [c[4] for c in pools] == ["github", "github-production"]
    providers = [c[1:] for c in find(calls, "create-oidc")]
    assert providers == [
        ["iam", "workload-identity-pools", "providers", "create-oidc", "github"]
        + ["--workload-identity-pool", pool, "--issuer-uri", ISSUER]
        + ["--attribute-mapping", mapping, "--attribute-condition", condition]
        + POOL_FLAGS
        for pool, mapping, condition in [
            ("github", MAPPING, FROM_REPO),
            ("github-production", PRODUCTION_MAPPING, PRODUCTION_CONDITION),
        ]
    ]


def test_display_names_fit_the_limit(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    names = [flag(c, "--display-name") for c in calls if "--display-name" in c]
    assert len(names) == 5
    assert all(len(name) <= 32 for name in names), names


def test_first_run_binds_each_identity_exactly(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    accounts = find(calls, "service-accounts", "add-iam-policy-binding")
    assert [(c[4], flag(c, "--member"), flag(c, "--role")) for c in accounts] == [
        (PLAN, PLAN_MEMBER, "roles/iam.workloadIdentityUser"),
        (APPLY, APPLY_MEMBER, "roles/iam.workloadIdentityUser"),
    ]
    project = find(calls, "projects", "add-iam-policy-binding")
    assert [(flag(c, "--member"), flag(c, "--role")) for c in project] == [
        (f"serviceAccount:{PLAN}", "roles/browser"),
        (f"serviceAccount:{PLAN}", "roles/iam.serviceAccountViewer"),
        (f"serviceAccount:{PLAN}", "roles/cloudquotas.viewer"),
        (f"serviceAccount:{PLAN}", "roles/serviceusage.serviceUsageViewer"),
        (f"serviceAccount:{APPLY}", "roles/resourcemanager.projectIamAdmin"),
        (f"serviceAccount:{APPLY}", "roles/iam.serviceAccountAdmin"),
        (f"serviceAccount:{APPLY}", "roles/serviceusage.serviceUsageAdmin"),
        (f"serviceAccount:{APPLY}", "roles/cloudquotas.admin"),
    ]
    bucket = find(calls, "buckets", "add-iam-policy-binding")
    assert [(c[4], flag(c, "--member"), flag(c, "--role")) for c in bucket] == [
        ("gs://demo-terraform-state", f"serviceAccount:{PLAN}", "roles/storage.objectViewer"),
        ("gs://demo-terraform-state", f"serviceAccount:{APPLY}", "roles/storage.objectAdmin"),
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


def test_budget_is_last_and_hides_the_ids(tmp_path: Path) -> None:
    result, calls = run(tmp_path)
    assert calls[-1][:4] == ["gcloud", "billing", "budgets", "create"]
    assert flag(calls[-1], "--budget-amount") == "75"
    for call in find(calls, "budgets"):
        assert flag(call, "--billing-project") == "demo"
    assert "stub-billing" not in result.stdout + result.stderr
    assert NUMBER not in result.stdout + result.stderr


def test_second_run_updates_and_creates_nothing(tmp_path: Path) -> None:
    run(tmp_path)
    result, calls = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if any(a in ("create", "create-oidc", "POST") for a in c)]
    assert [c[1:] for c in find(calls, "update-oidc")] == [
        ["iam", "workload-identity-pools", "providers", "update-oidc", "github"]
        + ["--workload-identity-pool", pool, "--issuer-uri", ISSUER]
        + ["--attribute-mapping", mapping, "--attribute-condition", condition]
        + POOL_FLAGS
        for pool, mapping, condition in [
            ("github", MAPPING, FROM_REPO),
            ("github-production", PRODUCTION_MAPPING, PRODUCTION_CONDITION),
        ]
    ]
    assert "kept" in result.stdout


def test_half_finished_run_finishes_on_the_next(tmp_path: Path) -> None:
    first, _ = run(tmp_path, fail=["create-oidc"])
    assert first.returncode != 0
    result, calls = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert [c[4] for c in find(calls, "workload-identity-pools", "create")] == ["github-production"]
    assert [flag(c, "--workload-identity-pool") for c in find(calls, "create-oidc")] == [
        "github",
        "github-production",
    ]


def test_retry_outlasts_two_failures(tmp_path: Path) -> None:
    result, calls = run(tmp_path, flaky={"projects add-iam-policy-binding": 2})
    assert result.returncode == 0, result.stderr
    assert len(find(calls, "projects", "add-iam-policy-binding")) == 10
    assert len([c for c in calls if c[0] == "sleep"]) == 2


def test_missing_project_prints_usage(tmp_path: Path) -> None:
    result, calls = run(tmp_path, project=None)
    assert result.returncode == 2
    assert "usage" in result.stderr
    assert calls == []


def test_settings_are_read_from_dotenv_without_running_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    dotenv = f"GCP_PROJECT=demo\r\ntouch {marker}\r\nBUDGET=20\r\nOTHER=1\r\n"
    result, calls = run(tmp_path, project=None, dotenv=dotenv)
    assert result.returncode == 0, result.stderr
    assert flag(calls[-1], "--budget-amount") == "20"
    assert flag(calls[-1], "--filter-projects") == "projects/demo"
    assert not marker.exists()


def test_environment_wins_over_dotenv(tmp_path: Path) -> None:
    result, calls = run(
        tmp_path, dotenv="GCP_PROJECT=other\nBUDGET=20\n", env_extra={"BUDGET": "9"}
    )
    assert result.returncode == 0, result.stderr
    assert flag(calls[-1], "--budget-amount") == "9"
    assert flag(calls[-1], "--filter-projects") == "projects/demo"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"visibility": "private"}, "public"),
        ({"user": "999"}, "owner"),
        ({"fail": ["-X PUT"]}, ""),
    ],
)
def test_github_problems_stop_before_google(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    result, calls = run(tmp_path, **overrides)
    assert result.returncode != 0
    assert message in result.stderr
    assert [c for c in calls if c[0] == "gcloud"] == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"gate": "false "}, "reviewer"),
        ({"gate": f"false {OWNER_ID},999"}, "reviewer"),
        ({"gate": f"true {OWNER_ID}"}, "reviewer"),
        ({"gate": ""}, "reviewer"),
        ({"gate": "null"}, "reviewer"),
        ({"branches": ["main", "dev"]}, "reviewer"),
        ({"fail": ["production --jq"]}, ""),
    ],
)
def test_weak_environment_stops_before_google(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    result, calls = run(tmp_path, **overrides)
    assert result.returncode == 1
    assert message in result.stderr
    assert [c for c in calls if c[0] == "gcloud"] == []


def test_environment_is_read_back_exactly(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    reads = [c for c in calls if c[:3] == ["gh", "api", "repos/owner/repo/environments/production"]]
    assert [c[3:] for c in reads] == [
        [
            "--jq",
            '"\\(.can_admins_bypass) \\([.protection_rules[]\n'
            '    | select(.type == "required_reviewers") | .reviewers[].reviewer.id'
            ' | tostring] | join(","))"',
        ]
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [({"account": ""}, "account"), ({"billing": ""}, "billing")],
)
def test_google_problems_stop_before_changes(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    result, calls = run(tmp_path, **overrides)
    assert result.returncode == 1
    assert message in result.stderr
    assert [c for c in calls if c[:2] in (["gcloud", "iam"], ["gcloud", "services"])] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"members": {PLAN: ["principal://example/other"]}},
        {"members": {APPLY: ["principal://example/other"]}},
        {"members": {PLAN: [PLAN_MEMBER + "9"]}},
        {"members": {APPLY: [PLAN_MEMBER]}},
        {"rows": {PLAN: ["roles/iam.serviceAccountTokenCreator\tprincipal://example/other"]}},
        {"rows": {APPLY: ["roles/iam.serviceAccountTokenCreator\tprincipal://example/other"]}},
        {"fail": ["get-iam-policy"]},
        {"keys": {PLAN: ["key-1"]}},
        {"keys": {APPLY: ["key-1"]}},
        {"fail": ["keys list"]},
        {"providers": {"github": ["other"]}},
        {"providers": {"github-production": ["other"]}},
        {"fail": ["providers list"]},
    ],
)
def test_unexpected_access_stops_before_any_binding(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    result, calls = run(tmp_path, **overrides)
    assert result.returncode != 0
    assert bindings(calls) == []
    assert find(calls, "secret") == []


@pytest.mark.parametrize("pool", ["github", "github-production"])
def test_extra_provider_is_reported_at_once(tmp_path: Path, pool: str) -> None:
    result, calls = run(tmp_path, providers={pool: ["other"]})
    assert result.returncode == 1
    assert "one provider" in result.stderr
    assert [c for c in calls if c[0] == "sleep"] == []


def test_keys_are_listed_for_user_managed_only(tmp_path: Path) -> None:
    _, calls = run(tmp_path)
    keys = find(calls, "keys", "list")
    assert [(flag(c, "--iam-account"), flag(c, "--managed-by")) for c in keys] == [
        (PLAN, "user"),
        (APPLY, "user"),
    ]


def test_output_hides_the_project_and_the_account(tmp_path: Path) -> None:
    result, _ = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "repo owner/repo" in result.stdout
    assert "demo" not in result.stdout + result.stderr
    assert "owner-account" not in result.stdout + result.stderr


def test_runs_from_another_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result, calls = run(tmp_path, project=None, dotenv="GCP_PROJECT=demo\n", cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert flag(calls[-1], "--filter-projects") == "projects/demo"


def test_cdpath_cannot_move_the_script(tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    (decoy / "scripts").mkdir(parents=True)
    (decoy / "Makefile").write_text("")
    (decoy / ".env").write_text("GCP_PROJECT=decoy\n")
    result, calls = run(
        tmp_path, project=None, dotenv="GCP_PROJECT=demo\n", env_extra={"CDPATH": str(decoy)}
    )
    assert result.returncode == 0, result.stderr
    assert flag(calls[-1], "--filter-projects") == "projects/demo"


def test_dotenv_without_a_makefile_is_ignored(tmp_path: Path) -> None:
    result, calls = run(tmp_path, project=None, dotenv="GCP_PROJECT=demo\n", makefile=False)
    assert result.returncode == 2
    assert calls == []


def test_dotenv_value_with_equals_is_read_whole(tmp_path: Path) -> None:
    result, calls = run(tmp_path, dotenv="BUDGET=2=0\n")
    assert result.returncode == 1
    assert "BUDGET" in result.stderr
    assert calls == []


@pytest.mark.parametrize(
    ("dotenv", "env_extra", "name"),
    [
        (None, {"GCP_PROJECT": "Demo"}, "GCP_PROJECT"),
        (None, {"GCP_REGION": "us central1"}, "GCP_REGION"),
        (None, {"BUDGET": "75USD"}, "BUDGET"),
        ('BUDGET="75"\n', None, "BUDGET"),
        ("GCP_REGION=europe-west1 # mine\n", None, "GCP_REGION"),
    ],
)
def test_bad_settings_stop_before_any_call(
    tmp_path: Path, dotenv: str | None, env_extra: dict[str, str] | None, name: str
) -> None:
    result, calls = run(tmp_path, dotenv=dotenv, env_extra=env_extra)
    assert result.returncode == 1
    assert name in result.stderr
    assert "literally" in result.stderr
    assert calls == []
