terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_storage_bucket" "rate_card" {
  name                        = var.bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  versioning {
    enabled = true
  }
}

resource "google_storage_bucket_object" "rate_card" {
  name          = var.object_name
  bucket        = google_storage_bucket.rate_card.name
  source        = var.rate_card_source
  content_type  = "application/json"
  cache_control = "private, max-age=300"
}

resource "google_storage_bucket_iam_member" "readers" {
  for_each = toset(var.reader_members)

  bucket = google_storage_bucket.rate_card.name
  role   = "roles/storage.objectViewer"
  member = each.value
}
