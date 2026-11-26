terraform {
  required_version = "~> 1.17.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.12.0"
    }
  }

  # The bucket is named at init, so no project id is committed.
  backend "gcs" {
    prefix = "google"
  }
}

provider "google" {
  project = var.project_id
}
