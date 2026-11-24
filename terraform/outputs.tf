output "bucket_name" {
  description = "Name of the private GCS bucket hosting the rate card."
  value       = google_storage_bucket.rate_card.name
}

output "rate_card_gs_uri" {
  description = "GCS URI of the published rate_card.json object."
  value       = "gs://${google_storage_bucket.rate_card.name}/${google_storage_bucket_object.rate_card.name}"
}

output "install_command" {
  description = "Non-interactive installer command for developer workstations using ADC."
  value       = "./scripts/install.sh --rate-card-url gs://${google_storage_bucket.rate_card.name}/${google_storage_bucket_object.rate_card.name} --mode wrapper --non-interactive"
}
