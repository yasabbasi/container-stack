variable "project_id" {
  description = "The Google Cloud project that holds the stack."
  type        = string
}

variable "github_repository" {
  description = "This repo as owner/name. Workflow files in it are what identities are bound to."
  type        = string
}
