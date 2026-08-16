"""
core/mapper.py
Turns the raw resources from discovery.py into the "target state" dictionary
that generator.py knows how to write out as Terraform.

The rules live in registry.py, not here. This file only knows how to *apply*
them, plus the handful of things that cannot be written as a simple rule:
  - names that are legal on one cloud but not on the other
  - regions, which have completely different names on each cloud
  - DNS records, which need their own small translation function

Anything we cannot translate is handed to the reporter instead of being
dropped quietly.

The shape we produce is:

    {
      "storage_accounts": [ {...}, ... ],
      "dns_zones":        [ {..., "records": [ {...}, ... ]}, ... ],
    }
"""

import logging
import re
from typing import Any, Dict, List

from core.registry import (
    MAPPING_REGISTRY,
    AWS_TO_AZURE_REGION_MAP,
    AZURE_TO_AWS_REGION_MAP,
)

logger = logging.getLogger("core.mapper")

# Used when the source region has no entry in the mapping table.
FALLBACK_REGION = {"aws2azure": "uksouth", "azure2aws": "eu-west-2"}

# Record types each cloud's DNS service can actually store.
AZURE_RECORD_TYPES = {"a", "aaaa", "caa", "cname", "mx", "ns", "ptr", "srv", "txt"}
AWS_RECORD_TYPES = {"a", "aaaa", "caa", "cname", "ds", "mx", "naptr",
                    "ns", "ptr", "spf", "srv", "txt"}

# Settings that are listed as "ignored" in the registry but that a reviewer
# would definitely want to know about, because losing them changes security.
SECURITY_SENSITIVE = {"encryption", "policy", "acl", "logging",
                      "public_access_block", "public_network_access_enabled"}

# The field generator.py cannot write a resource without. If the source did
# not give us one there is nothing sensible to generate, so we drop the
# resource and say so rather than crashing later on.
REQUIRED_FIELDS = {
    "azurerm_storage_account": "name",
    "aws_s3_bucket": "bucket",
    "azurerm_dns_zone": "name",
    "aws_route53_zone": "name",
}

# Route 53 writes a wildcard record's name using its octal escape code.
WILDCARD_ESCAPE = "\\052"

# What each target resource will accept as a tag. The limits are per resource
# type rather than per cloud because they genuinely differ: a hosted zone
# takes ten tags, an S3 bucket fifty.
TAG_LIMITS = {
    "aws_s3_bucket":            {"max_tags": 50, "key_max": 128, "value_max": 256},
    "aws_route53_zone":         {"max_tags": 10, "key_max": 128, "value_max": 256},
    "azurerm_storage_account":  {"max_tags": 50, "key_max": 128, "value_max": 256},
    "azurerm_dns_zone":         {"max_tags": 50, "key_max": 512, "value_max": 256},
}

# AWS allows letters, digits, spaces and + - = . _ : / @ only.
AWS_TAG_DISALLOWED = re.compile(r"[^a-zA-Z0-9\s+\-=._:/@]")
# Azure forbids these characters in a tag name. Values are unrestricted.
AZURE_TAG_DISALLOWED = re.compile(r"[<>%&\\?/]")


class SchemaMappingEngine:

    def __init__(self, direction: str, reporter: Any):
        self.direction = direction
        self.reporter = reporter
        self.rules = MAPPING_REGISTRY.get(direction, {})
        # Terraform resource addresses must be unique, so we remember every
        # name we have already handed out and add a number to any repeat.
        self.used_tf_names = set()

        if not self.rules:
            logger.warning(f"No mapping rules found for direction '{direction}'.")

    # ------------------------------------------------------------------
    # Small helpers for names
    # ------------------------------------------------------------------

    def _unique_tf_name(self, name: str, suffix: str = "") -> str:
        """
        Makes a Terraform-safe label, and makes sure we never use the same one
        twice. Two Route 53 records can share a name and type (for example the
        two halves of a weighted pair), and Terraform refuses to plan if two
        resources end up with the same address.

        The suffix (the record type) is added after cleaning, so a name that
        already ends in a dot does not leave a double underscore behind.
        """
        clean = name.replace(WILDCARD_ESCAPE, "wildcard").replace("*", "wildcard")
        clean = clean.replace("@", "apex")
        clean = re.sub(r"[^a-zA-Z0-9_]", "_", clean.rstrip(".").lower())
        clean = clean.strip("_") or "unnamed"

        if suffix:
            clean = f"{clean}_{suffix}"

        # A Terraform label may not start with a digit.
        if clean[0].isdigit():
            clean = f"r_{clean}"

        candidate = clean
        counter = 2
        while candidate in self.used_tf_names:
            candidate = f"{clean}_{counter}"
            counter += 1

        self.used_tf_names.add(candidate)
        return candidate

    @staticmethod
    def _azure_storage_name(name: str) -> str:
        """Azure storage accounts: 3-24 characters, lowercase letters and digits."""
        clean = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(clean) < 3:
            return f"migrated{clean}sa"
        return clean[:24]

    @staticmethod
    def _aws_bucket_name(name: str) -> str:
        """S3 buckets: 3-63 characters, lowercase letters, digits and hyphens."""
        clean = re.sub(r"[^a-z0-9\-]", "", name.lower().replace("_", "-"))
        clean = clean.strip("-")
        if len(clean) < 3:
            return f"migrated-{clean}"
        return clean[:63]

    def _translate_tags(self, resource_id: str, value: Any,
                        target_type: str) -> Dict[str, str]:
        """
        Cleans a tag dictionary so the target cloud will accept it.

        Discovery hands us a plain {key: value} dictionary whichever cloud it
        read, so the only work here is the target's own rules: which
        characters are legal in a key, how long keys and values may be, and
        how many tags one resource may carry.

        A tag that has to be changed is reported rather than changed quietly,
        because a tag is often what a cost report or an access policy keys
        off, and a renamed tag is a silently broken one.
        """
        limits = TAG_LIMITS.get(target_type)
        if limits is None:
            return {}

        # Be forgiving about the shape: a hand-built fixture, or a source we
        # add later, may still hand us AWS's list of Key/Value pairs.
        if isinstance(value, list):
            source = {}
            for item in value:
                if isinstance(item, dict) and item.get("Key"):
                    source[str(item["Key"])] = str(item.get("Value", ""))
        elif isinstance(value, dict):
            source = {str(k): str(v) for k, v in value.items()}
        else:
            self.reporter.log_dropped(
                resource_id, "tags", value,
                "The tags were not in a shape we understand, so none were "
                "copied. A dictionary or a list of Key/Value pairs is expected.")
            return {}

        going_to_aws = target_type.startswith("aws_")
        pattern = AWS_TAG_DISALLOWED if going_to_aws else AZURE_TAG_DISALLOWED

        clean_tags: Dict[str, str] = {}

        for key in sorted(source):
            original_value = source[key]

            clean_key = pattern.sub("_", key)[: limits["key_max"]].strip()
            # AWS reserves this prefix for its own tags and rejects any
            # attempt to set one.
            if going_to_aws and clean_key.lower().startswith("aws:"):
                self.reporter.log_dropped(
                    resource_id, f"tag '{key}'", original_value,
                    "AWS reserves the 'aws:' prefix for its own tags and will "
                    "not accept one, so this tag was dropped.")
                continue

            if not clean_key:
                self.reporter.log_dropped(
                    resource_id, f"tag '{key}'", original_value,
                    "Nothing legal was left of the tag key after the target "
                    "cloud's character rules were applied.")
                continue

            clean_value = original_value
            if going_to_aws:
                clean_value = pattern.sub("_", clean_value)
            clean_value = clean_value[: limits["value_max"]]

            if clean_key != key or clean_value != original_value:
                self.reporter.log_naming_mutation(
                    resource_id, f"tag '{key}' = '{original_value}'",
                    f"{clean_key} = {clean_value}",
                    f"{'AWS' if going_to_aws else 'Azure'} restricts the "
                    "characters and the length a tag may use.")

            # Two different keys can clean down to the same thing. Keeping the
            # first and reporting the second is better than one silently
            # overwriting the other.
            if clean_key in clean_tags:
                self.reporter.log_dropped(
                    resource_id, f"tag '{key}'", original_value,
                    f"After cleaning, this tag's key matched '{clean_key}', "
                    "which was already taken by an earlier tag.")
                continue

            clean_tags[clean_key] = clean_value

        if len(clean_tags) > limits["max_tags"]:
            keep = dict(list(clean_tags.items())[: limits["max_tags"]])
            dropped = {k: v for k, v in clean_tags.items() if k not in keep}
            self.reporter.log_dropped(
                resource_id, "tags", dropped,
                f"A {target_type} accepts at most {limits['max_tags']} tags. "
                f"The first {limits['max_tags']} by name were kept.")
            clean_tags = keep

        return clean_tags

    def _translate_region(self, resource_id: str, value: str) -> str:
        """Looks the region up in the right table, or falls back to a default."""
        if self.direction == "aws2azure":
            table = AWS_TO_AZURE_REGION_MAP
        else:
            table = AZURE_TO_AWS_REGION_MAP

        fallback = FALLBACK_REGION[self.direction]
        key = str(value).strip().lower()

        if key not in table:
            self.reporter.log_regional_fallback(resource_id, value, fallback)
            return fallback
        return table[key]

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def translate(self, raw_resources: List[Dict[str, Any]]) -> Dict[str, Any]:
        logger.info("Translating discovered resources into the target schema...")
        target_state = {"storage_accounts": [], "dns_zones": []}

        for resource in raw_resources:
            source_type = resource.get("type")
            resource_id = resource.get("id", "unknown")
            properties = resource.get("properties", {})

            rule = self.rules.get(source_type)
            if rule is None:
                # We know nothing about this resource, so we cannot translate
                # it. Record it rather than letting it disappear.
                self.reporter.log_dropped(
                    resource_id, source_type, "",
                    "No mapping rule in registry.py for this resource type.")
                continue

            target_type = rule["target_type"]
            # The zone or bucket name reads better as a Terraform label than
            # an internal id such as "/hostedzone/Z123456".
            label_source = properties.get("name") or properties.get("bucket") or resource_id
            mapped = {
                "tf_name": self._unique_tf_name(str(label_source)),
                "raw_id": resource_id,
            }

            # 1. Values the target provider always needs (account_tier, etc).
            #    These come first so a real discovered value can overwrite them.
            mapped.update(rule.get("target_defaults", {}))

            # 2. Straight attribute copies, with a few special cases.
            self._apply_attributes(rule, properties, mapped, target_type, resource_id)

            # 3. Nested blocks (versioning) and DNS records.
            self._apply_structures(rule, properties, mapped, resource_id)

            # 4. Report anything we did not touch.
            self._report_unmapped(rule, properties, resource_id)

            # 5. Refuse to pass on anything the generator cannot write.
            required = REQUIRED_FIELDS.get(target_type)
            if required and not mapped.get(required):
                self.reporter.log_dropped(
                    resource_id, required, properties,
                    f"The source resource had no value for '{required}', so no "
                    f"{target_type} could be created.")
                continue

            # 6. File the finished object under the right heading.
            if target_type in ("azurerm_storage_account", "aws_s3_bucket"):
                target_state["storage_accounts"].append(mapped)
            elif target_type in ("azurerm_dns_zone", "aws_route53_zone"):
                target_state["dns_zones"].append(mapped)
            else:
                self.reporter.log_dropped(
                    resource_id, target_type, "",
                    "generator.py cannot write this target type yet.")

        logger.info(f"Translated {len(target_state['storage_accounts'])} storage "
                    f"account(s) and {len(target_state['dns_zones'])} DNS zone(s).")
        return target_state

    # ------------------------------------------------------------------
    # Step 2: simple attributes
    # ------------------------------------------------------------------

    def _apply_attributes(self, rule, properties, mapped, target_type, resource_id):
        for source_attr, target_attr in rule.get("attribute_mappings", {}).items():
            if source_attr not in properties:
                continue

            value = properties[source_attr]

            # Regions are named differently on each cloud.
            if source_attr in ("region", "location"):
                value = self._translate_region(resource_id, value)

            # Tags need cleaning against the target's rules, and may end up
            # empty. An empty tags block is only noise in the HCL, so it is
            # left out entirely rather than written as "tags = {}".
            if source_attr == "tags":
                clean_tags = self._translate_tags(resource_id, value, target_type)
                if clean_tags:
                    mapped[target_attr] = clean_tags
                continue

            # Storage account and bucket names have strict character rules.
            if target_type == "azurerm_storage_account" and target_attr == "name":
                new_value = self._azure_storage_name(str(value))
                if new_value != value:
                    self.reporter.log_naming_mutation(
                        resource_id, str(value), new_value,
                        "Azure allows 3-24 lowercase letters and digits only.")
                value = new_value

            if target_type == "aws_s3_bucket" and target_attr == "bucket":
                new_value = self._aws_bucket_name(str(value))
                if new_value != value:
                    self.reporter.log_naming_mutation(
                        resource_id, str(value), new_value,
                        "S3 allows lowercase letters, digits and hyphens only.")
                value = new_value

            mapped[target_attr] = value

    # ------------------------------------------------------------------
    # Step 3: nested blocks and DNS records
    # ------------------------------------------------------------------

    def _apply_structures(self, rule, properties, mapped, resource_id):
        for source_attr, meta in rule.get("structural_mappings", {}).items():
            if source_attr not in properties:
                continue

            value = properties[source_attr]

            # DNS records get their own function because they need far more
            # work than "copy this value into that block".
            if meta.get("handler_directive") == "process_dns_records":
                mapped["records"] = self._map_dns_records(
                    value, resource_id, str(properties.get("name", "")))
                continue

            block = meta["target_block"]
            key = meta["target_key"]
            mapped.setdefault(block, {})[key] = meta["value_transformation"](value)

    # ------------------------------------------------------------------
    # Step 4: gap detection
    # ------------------------------------------------------------------

    def _report_unmapped(self, rule, properties, resource_id):
        """
        Anything the registry does not mention is a hole in our rules, and
        anything it deliberately ignores is still something the user is losing.
        Both are written to the report so nothing disappears silently.
        """
        handled = set(rule.get("attribute_mappings", {}))
        handled |= set(rule.get("structural_mappings", {}))
        ignored = set(rule.get("ignored_attributes", []))

        for key, value in properties.items():
            if key in handled:
                continue

            if key in ignored:
                # Deliberate, but the user should still see it. Encryption and
                # access policies are raised higher because losing them
                # quietly is a security problem, not a tidy-up problem.
                severity = "High" if key in SECURITY_SENSITIVE else "Low"
                self.reporter.log_dropped(
                    resource_id, key, value,
                    "Listed in ignored_attributes: no equivalent is created on "
                    "the target cloud.", severity)
            elif not value:
                # The SDK returns an empty answer for settings that were never
                # configured. Nothing is actually being lost, so this is only
                # worth a note. Without this the report fills up with noise and
                # the real problems get buried.
                self.reporter.log_gap(
                    resource_id, key, value, "Unmapped Source Property", "Low",
                    "No rule in registry.py, but the source value was empty.")
            else:
                self.reporter.log_gap(
                    resource_id, key, value, "Unmapped Source Property", "High",
                    "registry.py has no rule for this property.")

    # ------------------------------------------------------------------
    # DNS records
    # ------------------------------------------------------------------

    def _record_name(self, raw_name: str, zone_name: str, resource_id: str):
        """
        Works out what the record should be called on the target cloud, and
        says whether it sits at the top ("apex") of the zone.

        AWS writes full names ending in a dot ("www.example.com."), Azure
        writes names relative to the zone ("www", or "@" for the apex).
        """
        name = raw_name.replace(WILDCARD_ESCAPE, "*").rstrip(".")
        zone = zone_name.rstrip(".")

        is_apex = name in ("@", "", zone)

        if self.direction == "aws2azure":
            if is_apex:
                self.reporter.log_architectural_shift(
                    resource_id, "Apex record name", "AWS full domain name",
                    "the Azure '@' symbol")
                return "@", True
            # Cut the zone off the end to get the relative name Azure wants.
            if name.endswith("." + zone):
                return name[: -(len(zone) + 1)], False
            return name, False

        # azure2aws: build the full name AWS wants.
        if is_apex:
            self.reporter.log_architectural_shift(
                resource_id, "Apex record name", "the Azure '@' symbol",
                "the AWS full domain name")
            return zone, True
        if name.endswith("." + zone):
            return name, False
        return f"{name}.{zone}", False

    def _map_dns_records(self, raw_records, zone_id: str, zone_name: str):
        translated = []

        for record in raw_records or []:
            record_type = str(record.get("type", "")).strip().lower()
            raw_name = str(record.get("name", ""))

            # Both clouds create their own SOA and top-level NS records when
            # the zone is created, so copying them across breaks the zone.
            if record_type == "soa":
                self.reporter.log_architectural_shift(
                    zone_id, "SOA record", "source managed",
                    "the target's own automatic SOA record")
                continue

            name, is_apex = self._record_name(raw_name, zone_name, zone_id)

            if record_type == "ns" and is_apex:
                self.reporter.log_architectural_shift(
                    zone_id, "Apex NS record", "source name servers",
                    "the target's own name servers")
                continue

            # An alias record points at another service inside the same cloud
            # (an AWS load balancer, or an Azure public IP). Its value is a
            # hostname even when the record type is A, so writing it out as a
            # normal A record would produce Terraform that cannot apply.
            if record.get("is_alias"):
                self.reporter.log_dropped(
                    zone_id, f"{name} {record_type.upper()} (alias)",
                    record.get("values"),
                    "Alias records point at a service inside the source cloud "
                    "and have no equivalent on the target. Point this at the "
                    "migrated service by hand.")
                continue

            # Anything other than simple routing is an AWS-only feature.
            policy = record.get("routing_policy")
            if policy is not None and policy != "Simple":
                self.reporter.log_gap(
                    zone_id, f"{name} {record_type.upper()}", policy,
                    "Architectural Shift", "High",
                    f"{policy} routing is not available on the target cloud. "
                    "Only the record values were copied.")

            # Does the target cloud support this record type at all?
            supported = AZURE_RECORD_TYPES if self.direction == "aws2azure" else AWS_RECORD_TYPES
            if record_type not in supported:
                self.reporter.log_dropped(
                    zone_id, f"{name} {record_type.upper()}", record.get("values"),
                    "The target DNS service does not support this record type.")
                continue

            # Some SDK responses wrap values in quotes. Strip them so we do not
            # end up with doubled quotes in the generated HCL.
            values = [str(v).strip('"') for v in record.get("values", []) if str(v).strip()]

            if not values:
                self.reporter.log_dropped(
                    zone_id, f"{name} {record_type.upper()}", record.get("values"),
                    "The record had no values, so it was skipped to keep the "
                    "generated Terraform valid.")
                continue

            # A CNAME may only ever point at one place.
            if record_type == "cname" and len(values) > 1:
                self.reporter.log_gap(
                    zone_id, f"{name} CNAME", values, "Invalid Record", "High",
                    "A CNAME can only have one value. The rest were dropped.")
                values = values[:1]

            translated.append({
                # Label is based on the translated name, so an apex record is
                # called "apex_mx" rather than being named after a bare dot.
                "tf_name": self._unique_tf_name(name, suffix=record_type),
                "name": name,
                "type": record_type,
                "ttl": int(record.get("ttl") or 300),
                "values": values,
            })

        return translated