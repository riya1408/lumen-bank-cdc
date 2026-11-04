# Image repository, with the keep-last-3 cleanup rule
resource "google_artifact_registry_repository" "lumen" {
  repository_id          = "lumen"
  location               = "asia-south1"
  format                 = "DOCKER"
  description            = "Lumen Bank images"
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-last-3"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }

  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "86400s"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The dashboard's own identity
resource "google_service_account" "dashboard_sa" {
  account_id   = "dashboard-sa"
  display_name = "Lumen dashboard"
}

# The secret's container only. The URI value stays outside Terraform.
resource "google_secret_manager_secret" "mongo_uri" {
  secret_id = "mongo-uri"
  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}

# dashboard-sa may read this one secret, nothing else
resource "google_secret_manager_secret_iam_member" "dashboard_reads_mongo_uri" {
  secret_id = google_secret_manager_secret.mongo_uri.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.dashboard_sa.email}"
}
