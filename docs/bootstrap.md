# Bootstrap

`scripts/bootstrap.sh` creates what Terraform needs before it can run. The owner runs it once.

## What it creates

- The `production` environment on GitHub: the owner as required reviewer, only `main` may
  deploy, administrators can't bypass. This comes first, and the script stops unless the
  reviewer is set.
- The Terraform state bucket, with versioning.
- Two federation pools from GitHub Actions: `github` for every run of this repo, and
  `github-production` for jobs in the `production` environment on `main` only.
- The `terraform-plan` identity, for pull requests the owner opens, and the
  `terraform-apply` identity, for the `production` environment only.
- The repo secrets the workflows read.
- A monthly budget with alerts at 50, 80 and 100 percent. Alerts stop nothing.

## What it needs

- `gcloud`, signed in as the project's owner, and `gh`, signed in as the repo's owner.
- A public repo, since GitHub enforces required reviewers only on public repos on the free plan.
- A project with billing enabled.
- The settings in `.env.example`, in the environment or in `.env` beside the `Makefile`.
  Only `GCP_PROJECT` is required. `BUDGET` is in the billing account's currency.
  Values in `.env` are taken literally: no quotes, and no comment on the line.

## How to run it

```sh
GCP_PROJECT=your-project-id sh scripts/bootstrap.sh
```

## Running it again

It is safe to run again, and finishes a run that stopped halfway. What exists is not created
again, and both providers are updated to the same settings. Before it grants anything, it
stops if the environment's reviewer, admin bypass or branch differ from the above, if a pool
has a provider other than `github`, or if either identity has a user-managed key or any
policy member other than its one expected principal.
