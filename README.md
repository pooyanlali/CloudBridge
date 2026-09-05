# CloudBridge

A bidirectional infrastructure migration engine that translates object storage and DNS
configuration between Amazon Web Services and Microsoft Azure, and emits deployable
Terraform.

CloudBridge reads the configuration that is actually deployed in a source cloud by querying
provider APIs directly, resolves the structural and semantic differences between the two
platforms through a declarative mapping registry, and generates HashiCorp Configuration
Language for the target provider. Anything it cannot translate is written to a
severity-classified gap report rather than dropped silently.

Developed as the artefact for an MSc dissertation in Cloud and Network Computing at the
University of Staffordshire.

## Why

AWS and Azure model the same services through incompatible abstractions. An S3 bucket is a
top-level entity in a flat namespace; an Azure blob container requires a parent storage
account inside a resource group inside a subscription. Route 53 embeds routing policy in the
zone itself, while Azure externalises the equivalent into a separate service. Migrating
between them is therefore not a renaming exercise, and existing tools do not attempt it —
Terraformer and aztfy discover resources within a single provider and stop at the boundary.

Two design decisions follow from that.

**Live API discovery, not state parsing.** Tools that read `.tfstate` reproduce what
Terraform believes exists. Where an environment has drifted through manual changes, they
faithfully deploy a broken architecture into the target cloud and report no error.
CloudBridge queries the provider APIs, so drift is structurally absent rather than mitigated.

**Report gaps, don't drop them.** A tool that silently discards a property it cannot map
produces a target that looks migrated and behaves differently — an omitted block-public-access
policy is the canonical case. Detecting an untranslatable property and reporting it is
treated here as correct behaviour, not failure.

## What it migrates

| Source | Target | Covered |
|---|---|---|
| S3 buckets | Azure storage accounts and blob containers | Naming, region, versioning, tags |
| Route 53 hosted zones | Azure DNS zones | A, AAAA, CNAME, MX, TXT, SRV, NS, CAA |
| Azure storage accounts | S3 buckets | Naming, region, versioning, tags |
| Azure DNS zones | Route 53 hosted zones | As above |

**Configuration only.** CloudBridge migrates infrastructure definitions. It does not copy
objects between buckets and it does not cut over live DNS traffic. Run it against a source
holding a terabyte of data and you get a correctly configured, empty target. Data transfer
is a separate problem and needs a separate tool.

## Architecture

```
migrate.py          CLI entry point and pipeline orchestration
core/registry.py    declarative mapping rules and bidirectional region matrices
core/discovery.py   live SDK ingestion from the source provider
core/mapper.py      schema translation, naming, and gap classification
core/generator.py   HCL emission
core/deployer.py    Terraform invocation
core/reporter.py    severity-classified audit reporting
```

The pipeline runs discovery → mapping → generation → deployment. The mapper resolves
discovered configuration against the registry into a canonical, provider-agnostic
intermediate representation, and the generator emits HCL from that representation alone.
Because the generator never sees the source provider, bidirectionality falls out of the
architecture rather than requiring a second translation path.

Translation rules live in `registry.py` as data, not control flow, so a mapping can be added
or audited without touching the engine that applies it.

## Requirements

- Python 3.9+
- Terraform 1.5+ on `PATH` (not needed with `--skip-deploy`)
- AWS credentials with read access to S3 and Route 53
- An Azure service principal or CLI login with read access to storage and DNS

```bash
pip install boto3 azure-identity azure-mgmt-storage azure-mgmt-dns
```

## Configuration

Credentials are read from the environment. Nothing is committed to the repository.

```bash
# AWS
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Azure
export AZURE_SUBSCRIPTION_ID=...
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
```

Azure authentication uses `DefaultAzureCredential`, which tries environment variables, then
Azure CLI login, then managed identity — so `az login` alone is enough for local use.

## Usage

```bash
# Generate Terraform without deploying
python migrate.py --direction aws2azure --skip-deploy

# Migrate Azure storage and DNS into AWS, prompting before apply
python migrate.py --direction azure2aws

# Non-interactive
python migrate.py --direction aws2azure --auto-approve
```

| Flag | Default | Purpose |
|---|---|---|
| `--direction` | required | `aws2azure` or `azure2aws` |
| `--aws-region` | `eu-west-2` | AWS region to read from or write to |
| `--output-dir` | `./migration_output` | Where generated files are written |
| `--skip-deploy` | off | Generate files only; do not run Terraform |
| `--auto-approve` | off | Skip the confirmation prompt before apply |

Deployment is never silent: the engine reports how many high-severity gaps were found and
asks for confirmation before `terraform apply` unless `--auto-approve` is given.

## Output

Each run writes to its own timestamped folder, so runs never overwrite one another.

```
migration_output/run_20260905_143022/
├── canonical_ir.json           intermediate representation
├── main.tf                     provider configuration
├── infrastructure.tf           translated resources
└── migration_gap_report.json   what could not be carried across
```

The gap report is the part worth reading. Each entry records the resource, the attribute, its
original value, the type of gap, a severity, and an explanation:

```json
{
  "resource": "aws_route53_record.api_A",
  "attribute": "routing_policy",
  "original_value": "Latency",
  "gap_type": "architectural_shift",
  "severity": "High",
  "note": "Azure requires Traffic Manager for latency routing. Record values were carried across; the routing behaviour was not."
}
```

Severity is `High` for anything that changes behaviour or security posture, `Medium` for
architectural shifts and name mutations, and `Low` for regional fallbacks and empty
properties. An unrecognised severity is treated as `High` so a typo cannot hide a gap.

## Known limitations

Stated plainly rather than discovered later.

- **Non-deterministic resource group naming (`aws2azure`).** The generated Azure resource
  group name carries a random suffix so repeated test runs do not collide. This means the
  engine cannot regenerate a configuration matching infrastructure it previously deployed, so
  the `aws2azure` direction suits an initial migration rather than repeated execution. The
  `azure2aws` direction is deterministic and regenerates byte-identically.
- **`force_destroy` on generated S3 buckets.** Convenient for teardown, dangerous in
  production — it permits `terraform destroy` to delete a non-empty bucket. Review before
  using against anything you care about.
- **Encryption configuration is not translated.** The two platforms implement default
  encryption differently, so it is reported at high severity rather than mapped inaccurately.
- **Private DNS zones are skipped.** Azure private zones need a different SDK; AWS private
  hosted zones would need an `azurerm_private_dns_zone` plus a VNet link that the generator
  does not emit.
- **Route 53 alias records are dropped** with a note, having no Azure equivalent.
- **Routing policies other than Simple** have their record values carried across while the
  policy itself is reported as untranslatable.
- **One region per run.** A Terraform provider block names a single region, so the majority
  region is selected and the rest reported at high severity. Provider aliases are not
  generated.
- **No import of existing target resources.** Migrating into a target where the resources
  already exist will surface as a conflict at plan or apply.
- **Scope is object storage and DNS.** Compute, identity, networking and databases are out of
  scope.

## Licence

See LICENCE