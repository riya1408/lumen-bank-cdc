import {
  to = google_artifact_registry_repository.lumen
  id = "projects/lumen-bank-cdc/locations/asia-south1/repositories/lumen"
}

import {
  to = google_service_account.dashboard_sa
  id = "projects/lumen-bank-cdc/serviceAccounts/dashboard-sa@lumen-bank-cdc.iam.gserviceaccount.com"
}

import {
  to = google_secret_manager_secret.mongo_uri
  id = "projects/lumen-bank-cdc/secrets/mongo-uri"
}

import {
  to = google_secret_manager_secret_iam_member.dashboard_reads_mongo_uri
  id = "projects/lumen-bank-cdc/secrets/mongo-uri roles/secretmanager.secretAccessor serviceAccount:dashboard-sa@lumen-bank-cdc.iam.gserviceaccount.com"
}
