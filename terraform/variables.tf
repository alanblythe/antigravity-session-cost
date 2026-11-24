variable "project_id" {
  description = "GCP project ID that will host the private rate-card bucket."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique GCS bucket name for the private rate card."
  type        = string
}

variable "region" {
  description = "GCS bucket location/region."
  type        = string
  default     = "us-central1"
}

variable "object_name" {
  description = "Object path inside the GCS bucket."
  type        = string
  default     = "rate_card.json"
}

variable "rate_card_source" {
  description = "Local path to the rate_card.json file to upload."
  type        = string
  default     = "../skills/antigravity-session-cost/config/rate_card.json"
}

variable "reader_members" {
  description = "IAM principals granted roles/storage.objectViewer (e.g. group:eng@example.com, user:dev@example.com, serviceAccount:sa@project.iam.gserviceaccount.com)."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for m in var.reader_members :
      m != "allUsers" && m != "allAuthenticatedUsers"
    ])
    error_message = "Public IAM principals (allUsers, allAuthenticatedUsers) are not allowed. Grant least-privilege user:, group:, domain:, or serviceAccount: principals."
  }
}

variable "force_destroy" {
  description = "Allow Terraform/OpenTofu to delete the bucket even if versioned objects remain."
  type        = bool
  default     = false
}
