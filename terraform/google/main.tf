data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # The pools and their providers belong to scripts/bootstrap.sh, so they outlive a destroy.
  # Every run of this repo's workflows arrives through the pool named github.
  principals = join("/", [
    "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}",
    "locations/global/workloadIdentityPools/github",
  ])
  workflows = "${var.github_repository}/.github/workflows"
}

# reader: what the check workflow signs in as. It reads the project's setup and changes
# nothing. It has no role that reads a secret's value, a bucket's contents or data.
resource "google_service_account" "reader" {
  project      = var.project_id
  account_id   = "reader"
  display_name = "Read-only checks from the check workflow"
}

resource "google_project_iam_member" "reader" {
  for_each = toset([
    "roles/browser",
    "roles/cloudquotas.viewer",
    "roles/iam.serviceAccountViewer",
    "roles/serviceusage.serviceUsageViewer",
  ])

  project = var.project_id
  role    = each.value
  member  = google_service_account.reader.member
}

# Only check.yml as it is on main can become reader.
resource "google_service_account_iam_member" "reader_from_check" {
  service_account_id = google_service_account.reader.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.principals}/attribute.workflow_ref/${local.workflows}/check.yml@refs/heads/main"
}
