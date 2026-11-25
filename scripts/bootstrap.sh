#!/bin/sh
# One-time setup. The owner runs it from the clone, signed in to gcloud and gh. Safe to run again.
# It creates what Terraform needs before Terraform can run: the approval gate on GitHub, the
# state bucket, the federation from GitHub, the plan and apply identities, and the budget.
set -eu

# The environment wins over .env, which sits beside the Makefile.
root=$(dirname "$0")/..
env_project=${GCP_PROJECT:-} env_region=${GCP_REGION:-} env_budget=${BUDGET:-}
if [ -f "$root/Makefile" ] && [ -f "$root/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$root/.env"
    set +a
fi
GCP_PROJECT=${env_project:-${GCP_PROJECT:-}}
region=${env_region:-${GCP_REGION:-us-central1}}
budget=${env_budget:-${BUDGET:-75}}

[ -n "$GCP_PROJECT" ] || {
    echo "usage: GCP_PROJECT=<project id> [GCP_REGION=us-central1] [BUDGET=75] $0" >&2
    exit 2
}
bucket="$GCP_PROJECT-terraform-state"
domain="$GCP_PROJECT.iam.gserviceaccount.com"

step() { printf '\n== %s\n' "$*"; }
fail() {
    echo "$*" >&2
    exit 1
}

# A new account or pool takes a moment to be visible to the next command.
retry() {
    tries=0
    until "$@"; do
        tries=$((tries + 1))
        [ "$tries" -lt 6 ] || return 1
        sleep 5
    done
}

step "Checking GitHub"
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
read -r repo_id owner_id visibility <<EOF
$(gh api "repos/$repo" --jq '"\(.id) \(.owner.id) \(.visibility)"')
EOF
[ "$visibility" = public ] ||
    fail "$repo is not public. GitHub enforces an environment's required reviewers only on public repos on the free plan."
[ "$(gh api user --jq .id)" = "$owner_id" ] || fail "gh must be signed in as the owner of $repo"

step "The approval gate on GitHub"
environment="repos/$repo/environments/production"
gh api -X PUT "$environment" --input - >/dev/null <<JSON
{"wait_timer": 0, "prevent_self_review": false, "can_admins_bypass": false,
 "reviewers": [{"type": "User", "id": $owner_id}],
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
gh api "$environment/deployment-branch-policies" --jq '.branch_policies[].name' | grep -qx main ||
    gh api -X POST "$environment/deployment-branch-policies" -f name=main -f type=branch >/dev/null
gh api "$environment" --jq '.protection_rules[].type' | grep -qx required_reviewers ||
    fail "The production environment has no required reviewer. Nothing on Google Cloud was changed."

step "Checking Google Cloud"
number=$(gcloud projects describe "$GCP_PROJECT" --format 'value(projectNumber)')
email=$(gcloud config get-value account 2>/dev/null)
echo "repo $repo, project number $number"

step "Enabling the APIs"
gcloud services enable --project "$GCP_PROJECT" \
    iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
    cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
    cloudquotas.googleapis.com storage.googleapis.com billingbudgets.googleapis.com

step "The Terraform state bucket"
gcloud storage buckets describe "gs://$bucket" >/dev/null 2>&1 ||
    gcloud storage buckets create "gs://$bucket" --project "$GCP_PROJECT" \
        --location "$region" --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$bucket" --versioning >/dev/null

step "Federation from GitHub"
pools() { gcloud iam workload-identity-pools "$@" --project "$GCP_PROJECT" --location global; }

# One pool per kind of run, each with one provider named github: pool, mapping, condition.
federation() {
    pools describe "$1" >/dev/null 2>&1 || pools create "$1" --display-name "GitHub Actions ($1)"
    if pools providers describe github --workload-identity-pool "$1" >/dev/null 2>&1; then
        retry pools providers update-oidc github --workload-identity-pool "$1" \
            --attribute-mapping "$2" --attribute-condition "$3"
    else
        retry pools providers create-oidc github --workload-identity-pool "$1" \
            --issuer-uri "https://token.actions.githubusercontent.com" \
            --attribute-mapping "$2" --attribute-condition "$3"
    fi
}

# Tokens are accepted from this repo and this owner only, matched by ids that can't be reused.
from_repo="assertion.repository_id==\"$repo_id\" && assertion.repository_owner_id==\"$owner_id\""
federation github \
    'google.subject=assertion.sub,attribute.workflow_ref=assertion.workflow_ref,attribute.event_actor=assertion.event_name+":"+assertion.actor_id' \
    "$from_repo"
# Only jobs in the production environment on main, which wait for the owner's approval.
# A separate pool, so no other run can ever carry the attribute apply is bound to.
federation github-production \
    'google.subject=assertion.sub,attribute.environment=assertion.environment' \
    "$from_repo && assertion.environment==\"production\" && assertion.ref==\"refs/heads/main\""

step "The plan and apply identities"
accounts() { gcloud iam service-accounts "$@" --project "$GCP_PROJECT"; }
principals="principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools"

# name, display name, the one member allowed to act as it, then its project roles.
account() {
    name=$1 who="$1@$domain" member="$principals/$3"
    accounts describe "$who" >/dev/null 2>&1 || accounts create "$name" --display-name "$2"
    members=$(retry accounts get-iam-policy "$who" --flatten 'bindings[].members' \
        --filter 'bindings.role:roles/iam.workloadIdentityUser' --format 'value(bindings.members)')
    others=$(printf '%s\n' "$members" | grep -vxF -e "$member" -e '' || true)
    [ -z "$others" ] || fail "$who has unexpected roles/iam.workloadIdentityUser members: $others"
    retry accounts add-iam-policy-binding "$who" \
        --role roles/iam.workloadIdentityUser --member "$member" >/dev/null
    shift 3
    for role in "$@"; do
        retry gcloud projects add-iam-policy-binding "$GCP_PROJECT" --condition None \
            --member "serviceAccount:$who" --role "$role" >/dev/null
    done
}

bucket_grant() {
    retry gcloud storage buckets add-iam-policy-binding "gs://$bucket" \
        --member "serviceAccount:$1@$domain" --role "$2" >/dev/null
}

# plan: a pull request run started by the owner. It reads, and changes nothing.
account terraform-plan "Terraform plan on pull requests" \
    "github/attribute.event_actor/pull_request:$owner_id" \
    roles/browser roles/iam.serviceAccountViewer \
    roles/cloudquotas.viewer roles/serviceusage.serviceUsageViewer
bucket_grant terraform-plan roles/storage.objectViewer

# apply: the production environment only. It can grant roles, so in effect it owns the
# project. Wider roles for it are added by Terraform, in pull requests the owner approves.
account terraform-apply "Terraform apply after approval" \
    "github-production/attribute.environment/production" \
    roles/resourcemanager.projectIamAdmin roles/iam.serviceAccountAdmin \
    roles/serviceusage.serviceUsageAdmin roles/cloudquotas.admin
bucket_grant terraform-apply roles/storage.objectAdmin

step "The repo secrets"
# Secrets, not variables, so GitHub masks them in the public logs.
secret() { gh secret set "$1" --repo "$repo" --body "$2"; }
providers="projects/$number/locations/global/workloadIdentityPools"
secret GCP_PROJECT_ID "$GCP_PROJECT"
secret GCP_PROJECT_NUMBER "$number"
secret GCP_WIF_PROVIDER "$providers/github/providers/github"
secret GCP_WIF_PROVIDER_PRODUCTION "$providers/github-production/providers/github"
secret TF_STATE_BUCKET "$bucket"
secret OWNER_EMAIL "$email"

step "The budget and its alerts"
billing=$(gcloud billing projects describe "$GCP_PROJECT" --format 'value(billingAccountName)')
billing=${billing#billingAccounts/}
[ -n "$billing" ] || fail "The project has no billing account."
found=$(gcloud billing budgets list --billing-account "$billing" --billing-project "$GCP_PROJECT" \
    --filter 'displayName=container-stack' --format 'value(name)')
if [ -z "$found" ]; then
    gcloud billing budgets create --billing-account "$billing" --billing-project "$GCP_PROJECT" \
        --display-name container-stack --budget-amount "$budget" --calendar-period month \
        --filter-projects "projects/$GCP_PROJECT" --threshold-rule percent=0.50 \
        --threshold-rule percent=0.80 --threshold-rule percent=1.00 >/dev/null
fi
echo "Alerts at 50, 80 and 100 percent of $budget a month, in the billing account's currency."
echo "Alerts stop nothing. Google-side changes are now pull requests, applied after your approval."
