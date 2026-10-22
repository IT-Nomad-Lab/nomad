ui            = false
api_addr      = "http://127.0.0.1:8205"
cluster_addr  = "http://127.0.0.1:8206"

storage "raft" {
  path    = "/openbao/data"
  node_id = "nomad-1"
}

listener "tcp" {
  address     = "0.0.0.0:8200"   # container side; the host publishes it on 127.0.0.1 only
  tls_disable = true
}

audit "file" "to-file" {
  description = "NOMAD vault audit log"
  options {
    file_path = "/openbao/audit/audit.log"
  }
}
