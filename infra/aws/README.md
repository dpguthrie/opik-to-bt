# AWS runner

This creates an outbound-only Amazon Linux 2023 Graviton runner with Python 3.13
via `uv`, `bt` 0.14.0, and an encrypted 250 GB gp3 root volume. There is no
inbound SSH rule; connect with AWS Systems Manager.

```bash
terraform init
terraform apply -var 'repo_url=https://github.com/YOUR_ORG/opik-migrate.git'
aws ssm start-session --target "$(terraform output -raw instance_id)"
```

Override capacity only when needed:

```bash
terraform apply \
  -var 'repo_url=https://github.com/YOUR_ORG/opik-migrate.git' \
  -var 'instance_type=m7g.2xlarge' \
  -var 'root_volume_gb=500'
```

Inside the session, place credentials in `/opt/opik-migrate/.env` (mode `0600`)
or export them from your existing secret-management workflow, then run the same
commands documented in the main README.

API keys are intentionally not Terraform variables, so they do not enter
Terraform state. For production, attach a narrowly scoped Secrets Manager read
policy to the instance role and retrieve the secrets at runtime.

The root volume supports rolling partitions rather than storing the full
migration. Stop the instance to retain its checkpoint. Terraform destruction
also deletes the root volume, so copy `.opik-migrate/` elsewhere first if the
run must survive instance replacement.
