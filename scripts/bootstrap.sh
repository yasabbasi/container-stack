#!/bin/sh
# One-time setup. The owner runs it from the clone, signed in to gcloud and gh. Safe to run again.
# It creates what Terraform needs before Terraform can run: the approval gate on GitHub, the
# state bucket, the federation from GitHub, the plan and apply identities, and the budget.
set -eu

CDPATH='' cd -- "$(dirname "$0")/.."

# The environment wins over .env, which is read, never run.
project=${GCP_PROJECT:-} region=${GCP_REGION:-} budget=${BUDGET:-}
if [ -f Makefile ] && [ -f .env ]; then
    cr=$(printf '\r')
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%"$cr"}
        case $line in
            GCP_PROJECT=*) project=${project:-${line#*=}} ;;
            GCP_REGION=*) region=${region:-${line#*=}} ;;
            BUDGET=*) budget=${budget:-${line#*=}} ;;
        esac
    done <.env
fi
region=${region:-us-central1} budget=${budget:-75}

[ -n "$project" ] || {
    echo "usage: GCP_PROJECT=<project id> [GCP_REGION=us-central1] [BUDGET=75] sh scripts/bootstrap.sh" >&2
    exit 2
}

step() { printf '\n== %s\n' "$*"; }
fail() {
    echo "$*" >&2
    exit 1
}

# name, value, allowed characters.
setting() {
    case $2 in
        '' | *[!"$3"]*)
            fail "$1 may hold only $4. Values in .env are taken literally:" \
                "no quotes, and no comment on the line."
            ;;
    esac
}
setting GCP_PROJECT "$project" abcdefghijklmnopqrstuvwxyz0123456789- "lowercase letters, digits and hyphens"
setting GCP_REGION "$region" abcdefghijklmnopqrstuvwxyz0123456789- "lowercase letters, digits and hyphens"
setting BUDGET "$budget" 0123456789 digits
bucket="$project-terraform-state"
domain="$project.iam.gserviceaccount.com"

# A new account or pool can take a minute or more to be visible to the next command.
retry() {
    tries=0
    until "$@"; do
        tries=$((tries + 1))
        [ "$tries" -lt 18 ] || return 1
        sleep 5
    done
}

step "Checking GitHub"
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
info=$(gh api "repos/$repo" --jq '"\(.id) \(.owner.id) \(.visibility)"')
read -r repo_id owner_id visibility <<EOF
$info
EOF
[ "$visibility" = public ] ||
    fail "$repo is not public. GitHub enforces an environment's required reviewers only on public repos on the free plan."
[ "$(gh api user --jq .id)" = "$owner_id" ] || fail "gh must be signed in as the owner of $repo"

step "The approval gate on GitHub"
environment="repos/$repo/environments/production"
policies="$environment/deployment-branch-policies"
gh api -X PUT "$environment" --input - >/dev/null <<JSON
{"wait_timer": 0, "prevent_self_review": false, "can_admins_bypass": false,
 "reviewers": [{"type": "User", "id": $owner_id}],
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
branches() { gh api "$policies" --jq '.branch_policies[].name'; }
[ -n "$(branches)" ] || gh api -X POST "$policies" -f name=main -f type=branch >/dev/null
gate=$(gh api "$environment" --jq '"\(.can_admins_bypass) \([.protection_rules[]
    | select(.type == "required_reviewers") | .reviewers[].reviewer.id | tostring] | join(","))"')
if [ "$gate" != "false $owner_id" ] || [ "$(branches)" != main ]; then
    fail "The production environment must have the owner as its only required reviewer, no admin" \
        "bypass, and main as its only branch. Nothing on Google Cloud was changed."
fi

step "Checking Google Cloud"
number=$(gcloud projects describe "$project" --format 'value(projectNumber)')
email=$(gcloud config get-value account 2>/dev/null)
[ -n "$email" ] || fail "gcloud has no active account. Sign in with: gcloud auth login"
billing=$(gcloud billing projects describe "$project" --format 'value(billingAccountName)')
billing=${billing#billingAccounts/}
[ -n "$billing" ] || fail "The project has no billing account. Nothing on Google Cloud was changed."
echo "repo $repo"

step "Enabling the APIs"
gcloud services enable --project "$project" \
    iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
    cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
    cloudquotas.googleapis.com storage.googleapis.com billingbudgets.googleapis.com

step "The Terraform state bucket"
gcloud storage buckets describe "gs://$bucket" >/dev/null 2>&1 ||
    gcloud storage buckets create "gs://$bucket" --project "$project" \
        --location "$region" --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$bucket" --versioning >/dev/null

step "Federation from GitHub"
pools() { gcloud iam workload-identity-pools "$@" --project "$project" --location global; }
# A just-enabled API can fail a describe, which would look like "does not exist".
retry pools list >/dev/null

# A just-created provider can be missing from the list for a moment.
listed() {
    names=$(pools providers list --workload-identity-pool "$1" --format 'value(name.basename())') &&
        [ -n "$names" ]
}

# One pool per kind of run, each with one provider named github: pool, mapping, condition.
federation() {
    pools describe "$1" >/dev/null 2>&1 || pools create "$1" --display-name "GitHub: $1"
    if pools providers describe github --workload-identity-pool "$1" >/dev/null 2>&1; then
        verb=update-oidc
    else
        verb=create-oidc
    fi
    retry pools providers "$verb" github --workload-identity-pool "$1" \
        --issuer-uri "https://token.actions.githubusercontent.com" \
        --attribute-mapping "$2" --attribute-condition "$3"
    retry listed "$1" || fail "Pool $1 has no provider yet. Wait a minute, then run this again."
    [ "$names" = github ] || fail "Pool $1 must have one provider, github, and no other."
}

# Tokens are accepted from this repo and this owner only, matched by ids that can't be reused.
from_repo="assertion.repository_id==\"$repo_id\" && assertion.repository_owner_id==\"$owner_id\""
federation github \
    'google.subject=assertion.sub,attribute.workflow_ref=assertion.workflow_ref,attribute.event_actor=assertion.event_name+":"+assertion.actor_id' \
    "$from_repo"
# Only jobs in the production environment on main of the public repo, which wait for the
# owner's approval. A separate pool, so no other run can ever carry what apply is bound to.
federation github-production \
    'google.subject=assertion.sub,attribute.environment=assertion.environment' \
    "$from_repo && assertion.repository_visibility==\"public\" && assertion.environment==\"production\" && assertion.ref==\"refs/heads/main\""

step "The plan and apply identities"
accounts() { gcloud iam service-accounts "$@" --project "$project"; }
principals="principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools"
plan_member="$principals/github/attribute.event_actor/pull_request:$owner_id"
apply_member="$principals/github-production/attribute.environment/production"

create_account() {
    accounts describe "$1@$domain" >/dev/null 2>&1 || accounts create "$1" --display-name "$2"
}

# Before any grant: no keys, and no one but the expected member may act as the account.
check_account() {
    who="$1@$domain"
    keys=$(retry accounts keys list --iam-account "$who" --managed-by user --format 'value(name)')
    [ -z "$keys" ] || fail "$1 has user-managed keys. Delete them, then run this again."
    policy=$(retry accounts get-iam-policy "$who" --flatten 'bindings[].members' \
        --format 'value(bindings.role,bindings.members)')
    # With no bindings, gcloud prints one empty row: a lone tab.
    case $policy in *[![:space:]]*) ;; *) policy= ;; esac
    [ -z "$policy" ] || [ "$policy" = "$(printf 'roles/iam.workloadIdentityUser\t%s' "$2")" ] ||
        fail "$1 has an unexpected policy: $policy"
}

# name, member, bucket role, then project roles.
bind_account() {
    who="$1@$domain" member=$2 bucket_role=$3
    shift 3
    retry accounts add-iam-policy-binding "$who" \
        --role roles/iam.workloadIdentityUser --member "$member" >/dev/null
    for role in "$@"; do
        retry gcloud projects add-iam-policy-binding "$project" --condition None \
            --member "serviceAccount:$who" --role "$role" >/dev/null
    done
    retry gcloud storage buckets add-iam-policy-binding "gs://$bucket" \
        --member "serviceAccount:$who" --role "$bucket_role" >/dev/null
}

create_account terraform-plan "Terraform plan on pull requests"
create_account terraform-apply "Terraform apply after approval"
check_account terraform-plan "$plan_member"
check_account terraform-apply "$apply_member"

# plan: a pull request run started by the owner. It reads, and changes nothing.
bind_account terraform-plan "$plan_member" roles/storage.objectViewer \
    roles/browser roles/iam.serviceAccountViewer \
    roles/cloudquotas.viewer roles/serviceusage.serviceUsageViewer

# apply: the production environment only. It can grant roles, so in effect it owns the
# project. Wider roles for it are added by Terraform, in pull requests the owner approves.
bind_account terraform-apply "$apply_member" roles/storage.objectAdmin \
    roles/resourcemanager.projectIamAdmin roles/iam.serviceAccountAdmin \
    roles/serviceusage.serviceUsageAdmin roles/cloudquotas.admin

step "The repo secrets"
# Secrets, not variables, so GitHub masks them in the public logs.
secret() { gh secret set "$1" --repo "$repo" --body "$2"; }
providers="projects/$number/locations/global/workloadIdentityPools"
secret GCP_PROJECT_ID "$project"
secret GCP_PROJECT_NUMBER "$number"
secret GCP_WIF_PROVIDER "$providers/github/providers/github"
secret GCP_WIF_PROVIDER_PRODUCTION "$providers/github-production/providers/github"
secret TF_STATE_BUCKET "$bucket"
secret OWNER_EMAIL "$email"

step "The budget and its alerts"
found=$(gcloud billing budgets list --billing-account "$billing" --billing-project "$project" \
    --filter 'displayName=container-stack' --format 'value(name)')
if [ -z "$found" ]; then
    gcloud billing budgets create --billing-account "$billing" --billing-project "$project" \
        --display-name container-stack --budget-amount "$budget" --calendar-period month \
        --filter-projects "projects/$project" --threshold-rule percent=0.50 \
        --threshold-rule percent=0.80 --threshold-rule percent=1.00 >/dev/null
    echo "Alerts at 50, 80 and 100 percent of $budget a month, in the billing account's currency."
else
    echo "The existing budget was kept. Change its amount in the Cloud console."
fi
echo "Alerts stop nothing. Google-side changes are now pull requests, applied after your approval."
