terraform {
  backend "gcs" {
    bucket = "lumen-bank-cdc-tfstate"
    prefix = "lumen/infra"
  }
}
