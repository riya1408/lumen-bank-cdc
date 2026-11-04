# The live dashboard service
resource "google_cloud_run_v2_service" "dashboard" {
  name                = "lumen-dashboard"
  location            = "asia-south1"
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account  = google_service_account.dashboard_sa.email
    session_affinity = true

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "asia-south1-docker.pkg.dev/lumen-bank-cdc/lumen/dashboard:v1"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "1Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "MONGO_DB"
        value = "lumen_bank"
      }

      env {
        name = "MONGO_URI"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mongo_uri.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # New images are deployed outside Terraform; Terraform owns the settings.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }
}

# Anyone with the link can open the dashboard
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.dashboard.name
  location = google_cloud_run_v2_service.dashboard.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

import {
  to = google_cloud_run_v2_service.dashboard
  id = "projects/lumen-bank-cdc/locations/asia-south1/services/lumen-dashboard"
}

import {
  to = google_cloud_run_v2_service_iam_member.public
  id = "projects/lumen-bank-cdc/locations/asia-south1/services/lumen-dashboard roles/run.invoker allUsers"
}

