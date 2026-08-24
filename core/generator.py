"""
core/generator.py
Writes the target state produced by mapper.py out as Terraform files.

Two files are produced per run:
    main.tf             provider settings (and the resource group, on Azure)
    infrastructure.tf   the migrated zones, records, buckets and accounts

This file only formats text. It does not decide what to migrate; by the time
anything gets here the mapper has already made those decisions. The one thing
it does decide is which single region the provider block should use, because
Terraform providers are configured per region and our target state may contain
resources from several.
"""

import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("core.generator")

# Used when the target state contains no region we can copy.
DEFAULT_AWS_REGION = "eu-west-2"
DEFAULT_AZURE_LOCATION = "uksouth"

# A single string inside a DNS TXT record cannot be longer than this.
TXT_CHUNK_SIZE = 255
# Azure stores each TXT value as one string with this limit.
AZURE_TXT_LIMIT = 1024


def hcl_string(value: Any) -> str:
    """
    Wraps a Python value in quotes so it is safe to drop into HCL.

    As well as backslashes and quotes we have to escape "${" and "%{",
    because Terraform treats those as the start of an expression. A SPF
    record containing "${" would otherwise stop the plan with a syntax error.
    """
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("${", "$${").replace("%{", "%%{")
    return f'"{text}"'


def split_long_txt(value: str) -> str:
    """
    DNS only allows 255 characters per string, so a long TXT value has to be
    sent as several quoted strings joined together. Short values are left
    exactly as they are.
    """
    if len(value) <= TXT_CHUNK_SIZE:
        return value

    chunks = [value[i:i + TXT_CHUNK_SIZE]
              for i in range(0, len(value), TXT_CHUNK_SIZE)]
    return "".join(f'"{c}"' for c in chunks)


def hcl_tags(tags: Dict[str, Any], indent: str = "  ") -> List[str]:
    """
    Renders a tags map. All four target resource types take tags in exactly
    this form, so one function covers both clouds.

    Keys are quoted even when they would not have to be, because a tag key is
    allowed to contain characters that a bare HCL identifier is not, and
    sorted so that two runs over the same infrastructure produce the same
    file and the diff stays readable.
    """
    lines = [f'{indent}tags = {{']
    for key in sorted(tags):
        lines.append(f'{indent}  {hcl_string(key)} = {hcl_string(tags[key])}')
    lines.append(f'{indent}}}')
    return lines


def strip_trailing_dot(value: str) -> str:
    """Azure does not want the trailing dot that AWS puts on host names."""
    return str(value).rstrip(".")


class DeclarativeHCLGenerator:

    def __init__(self, output_dir: Path, direction: str, reporter: Any = None):
        self.output_dir = Path(output_dir)
        self.direction = direction
        self.reporter = reporter

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def generate(self, config: Dict[str, Any]) -> List[Path]:
        if self.direction == "aws2azure":
            location = self._pick_region(config, "location", DEFAULT_AZURE_LOCATION)
            return [self._write("main.tf", self._azure_main(location)),
                    self._write("infrastructure.tf", self._azure_infra(config, location))]

        if self.direction == "azure2aws":
            region = self._pick_region(config, "region", DEFAULT_AWS_REGION)
            return [self._write("main.tf", self._aws_main(region)),
                    self._write("infrastructure.tf", self._aws_infra(config))]

        raise ValueError(f"Unknown migration direction: {self.direction}")

    def _write(self, filename: str, text: str) -> Path:
        path = self.output_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _pick_region(self, config: Dict[str, Any], key: str, default: str) -> str:
        """
        A Terraform provider block names one region. If the migrated resources
        came from more than one we use the most common and tell the user about
        the rest, because the others will be created in the wrong place.
        """
        regions = [sa[key] for sa in config.get("storage_accounts", []) if sa.get(key)]
        if not regions:
            return default

        counts = Counter(regions)
        chosen = counts.most_common(1)[0][0]

        others = sorted(set(regions) - {chosen})
        if others and self.reporter:
            self.reporter.log_gap(
                "provider", key, ", ".join(others), "Architectural Shift", "High",
                f"Resources were found in more than one region. The provider is "
                f"set to '{chosen}', so the others will be created there instead. "
                f"Add provider aliases to keep them apart.")
        return chosen

    # ------------------------------------------------------------------
    # AWS output
    # ------------------------------------------------------------------

    def _aws_main(self, region: str) -> str:
        return (
            'terraform {\n'
            '  required_providers {\n'
            '    aws = {\n'
            '      source  = "hashicorp/aws"\n'
            '      version = "~> 5.0"\n'
            '    }\n'
            '  }\n'
            '}\n\n'
            'provider "aws" {\n'
            f'  region = {hcl_string(region)}\n'
            '}\n'
        )

    def _aws_infra(self, config: Dict[str, Any]) -> str:
        blocks = []

        # ---- Route 53 zones and their records ----
        for zone in config.get("dns_zones", []):
            zone_tf = zone["tf_name"]
            lines = [f'resource "aws_route53_zone" "{zone_tf}" {{',
                     f'  name = {hcl_string(zone["name"])}']
            if zone.get("tags"):
                lines.extend(hcl_tags(zone["tags"]))
            lines.append('}\n')
            blocks.append("\n".join(lines))

            for record in zone.get("records", []):
                blocks.append(self._aws_record(zone_tf, record))

        # ---- S3 buckets ----
        for bucket in config.get("storage_accounts", []):
            bucket_tf = bucket["tf_name"]
            lines = [f'resource "aws_s3_bucket" "{bucket_tf}" {{',
                     f'  bucket = {hcl_string(bucket["bucket"])}']
            if bucket.get("force_destroy"):
                lines.append('  force_destroy = true')
            if bucket.get("tags"):
                lines.extend(hcl_tags(bucket["tags"]))
            lines.append('}\n')
            blocks.append("\n".join(lines))

            # Versioning is its own resource in the AWS provider, not a block
            # inside the bucket.
            status = bucket.get("versioning", {}).get("status")
            if status == "Enabled":
                blocks.append(
                    f'resource "aws_s3_bucket_versioning" "{bucket_tf}_versioning" {{\n'
                    f'  bucket = aws_s3_bucket.{bucket_tf}.id\n'
                    f'  versioning_configuration {{\n'
                    f'    status = "Enabled"\n'
                    f'  }}\n'
                    f'}}\n'
                )

        return "\n".join(blocks)

    def _aws_record(self, zone_tf: str, record: Dict[str, Any]) -> str:
        """One aws_route53_record block."""
        values = record["values"]

        # Long TXT values have to be broken into 255 character strings.
        if record["type"] in ("txt", "spf"):
            values = [split_long_txt(v) for v in values]

        rendered = ", ".join(hcl_string(v) for v in values)
        return (
            f'resource "aws_route53_record" "{record["tf_name"]}" {{\n'
            f'  zone_id = aws_route53_zone.{zone_tf}.zone_id\n'
            f'  name    = {hcl_string(record["name"])}\n'
            f'  type    = {hcl_string(record["type"].upper())}\n'
            f'  ttl     = {record["ttl"]}\n'
            f'  records = [{rendered}]\n'
            f'}}\n'
        )

    # ------------------------------------------------------------------
    # Azure output
    # ------------------------------------------------------------------

    def _azure_main(self, location: str) -> str:
        # A short random suffix keeps repeated test runs from clashing over
        # the same resource group name.
        suffix = uuid.uuid4().hex[:6]
        return (
            'terraform {\n'
            '  required_providers {\n'
            '    azurerm = {\n'
            '      source  = "hashicorp/azurerm"\n'
            '      version = "~> 3.0"\n'
            '    }\n'
            '  }\n'
            '}\n\n'
            'provider "azurerm" {\n'
            '  features {}\n'
            '}\n\n'
            'resource "azurerm_resource_group" "migrated" {\n'
            f'  name     = "rg-migrated-infra-{suffix}"\n'
            f'  location = {hcl_string(location)}\n'
            '}\n'
        )

    def _azure_infra(self, config: Dict[str, Any], default_location: str) -> str:
        blocks = []

        # ---- DNS zones and their records ----
        for zone in config.get("dns_zones", []):
            zone_tf = zone["tf_name"]
            lines = [
                f'resource "azurerm_dns_zone" "{zone_tf}" {{',
                f'  name                = {hcl_string(zone["name"])}',
                '  resource_group_name = azurerm_resource_group.migrated.name',
            ]
            if zone.get("tags"):
                lines.extend(hcl_tags(zone["tags"]))
            lines.append('}\n')
            blocks.append("\n".join(lines))

            for record in zone.get("records", []):
                blocks.append(self._azure_record(zone_tf, record))

        # ---- Storage accounts ----
        for account in config.get("storage_accounts", []):
            account_tf = account["tf_name"]
            lines = [
                f'resource "azurerm_storage_account" "{account_tf}" {{',
                f'  name                     = {hcl_string(account["name"])}',
                '  resource_group_name      = azurerm_resource_group.migrated.name',
                f'  location                 = '
                f'{hcl_string(account.get("location", default_location))}',
                f'  account_tier             = '
                f'{hcl_string(account.get("account_tier", "Standard"))}',
                f'  account_replication_type = '
                f'{hcl_string(account.get("account_replication_type", "LRS"))}',
            ]

            if account.get("blob_properties"):
                lines.append('  blob_properties {')
                for key, value in account["blob_properties"].items():
                    if isinstance(value, bool):
                        lines.append(f'    {key} = {str(value).lower()}')
                    else:
                        lines.append(f'    {key} = {hcl_string(value)}')
                lines.append('  }')

            if account.get("tags"):
                lines.extend(hcl_tags(account["tags"]))

            lines.append('}\n')
            blocks.append("\n".join(lines))

        return "\n".join(blocks)

    def _azure_record(self, zone_tf: str, record: Dict[str, Any]) -> str:
        """
        One azurerm_dns_*_record block.

        Azure does not store every record type as a plain list of strings the
        way AWS does. MX, SRV and CAA each need their parts in named fields,
        so we split the AWS-style value string back up here.
        """
        record_type = record["type"]
        values = record["values"]

        lines = [
            f'resource "azurerm_dns_{record_type}_record" "{record["tf_name"]}" {{',
            f'  name                = {hcl_string(record["name"])}',
            f'  zone_name           = azurerm_dns_zone.{zone_tf}.name',
            '  resource_group_name = azurerm_resource_group.migrated.name',
            f'  ttl                 = {record["ttl"]}',
        ]

        if record_type == "txt":
            # One record block per string.
            for value in values:
                if len(value) > AZURE_TXT_LIMIT and self.reporter:
                    self.reporter.log_gap(
                        record["tf_name"], "txt value", len(value),
                        "Invalid Record", "High",
                        f"Azure allows {AZURE_TXT_LIMIT} characters per TXT "
                        "value; this one is longer and will be rejected.")
                lines.append(f'  record {{\n    value = {hcl_string(value)}\n  }}')

        elif record_type == "cname":
            # A CNAME is a single string, not a list.
            lines.append(f'  record              = '
                         f'{hcl_string(strip_trailing_dot(values[0]))}')

        elif record_type == "mx":
            # AWS format: "10 mail.example.com"
            for value in values:
                preference, exchange = self._split_value(value, 2, ["10", value])
                lines.append(
                    f'  record {{\n'
                    f'    preference = {hcl_string(preference)}\n'
                    f'    exchange   = {hcl_string(strip_trailing_dot(exchange))}\n'
                    f'  }}')

        elif record_type == "srv":
            # AWS format: "1 10 5060 sipserver.example.com"
            for value in values:
                priority, weight, port, target = self._split_value(
                    value, 4, ["0", "0", "0", value])
                lines.append(
                    f'  record {{\n'
                    f'    priority = {priority}\n'
                    f'    weight   = {weight}\n'
                    f'    port     = {port}\n'
                    f'    target   = {hcl_string(strip_trailing_dot(target))}\n'
                    f'  }}')

        elif record_type == "caa":
            # AWS format: 0 issue "letsencrypt.org"
            for value in values:
                flags, tag, caa_value = self._split_value(
                    value, 3, ["0", "issue", value])
                lines.append(
                    f'  record {{\n'
                    f'    flags = {flags}\n'
                    f'    tag   = {hcl_string(tag)}\n'
                    f'    value = {hcl_string(caa_value.strip(chr(34)))}\n'
                    f'  }}')

        else:
            # A, AAAA, NS and PTR are plain lists.
            rendered = ", ".join(
                hcl_string(strip_trailing_dot(v)) for v in values)
            lines.append(f'  records             = [{rendered}]')

        lines.append('}\n')
        return "\n".join(lines)

    @staticmethod
    def _split_value(value: str, parts: int, fallback: List[str]) -> List[str]:
        """
        Splits a record value into a fixed number of parts. If the value is
        not in the shape we expect we return the fallback rather than crash,
        so one odd record cannot stop the whole migration.
        """
        pieces = str(value).split(None, parts - 1)
        if len(pieces) != parts:
            logger.warning(f"Could not split DNS value '{value}' into {parts} parts.")
            return fallback
        return pieces