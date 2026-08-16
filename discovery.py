"""
core/discovery.py
Reads the resources that already exist in the source cloud (AWS or Azure)
and returns them as a list of plain dictionaries.

Every resource is returned in the same shape so that mapper.py can read it:

    {"type": "aws_s3_bucket", "id": "my-bucket", "properties": {...}}

DNS records are always returned in the same shape too, whichever cloud they
came from. The values are flat strings, because that is the one
format that carries every record type without losing anything

Tags are always returned as a plain {"key": "value"} dictionary of strings,
whichever cloud they came from, so nothing downstream has to know that AWS
keeps them as a list of {"Key": k, "Value": v} pairs.

A few things here are not obvious, so they are explained where they happen:
  - S3 buckets are global, but their settings must be read from their own region.
  - Route 53 only returns a limited number of results per request, so we page the results.
  - Azure keeps CNAME and SOA in a singular field, everything else in a list.
  - Azure stores long TXT values as a list of small pieces that must be joined.
"""

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger("core.discovery")

# Very old European buckets report "EU" instead of a normal region name.
OLD_REGION_NAMES = {"EU": "eu-west-1"}

# Route 53 returns long TXT values as quoted pieces, like: "part1" "part2"
# This pattern picks out the text inside each pair of quotes.
QUOTED_PIECE = re.compile(r'"((?:[^"\\]|\\.)*)"')

# S3 raises an error instead of returning an empty answer when a setting was
# never turned on. Those errors are normal and are not worth reporting. Any
# other error is a real problem, so it gets logged rather than swallowed.
NOT_CONFIGURED = {
    "NoSuchBucketPolicy",
    "NoSuchCORSConfiguration",
    "NoSuchLifecycleConfiguration",
    "NoSuchTagSet",
    "NoSuchWebsiteConfiguration",
    "NoSuchPublicAccessBlockConfiguration",
    "NoSuchOwnershipControls",
    "NoSuchConfiguration",
    "ReplicationConfigurationNotFoundError",
    "ServerSideEncryptionConfigurationNotFoundError",
    "ObjectLockConfigurationNotFoundError",
}

class SDKDiscoveryEngine:

    def __init__(self, region: str = "eu-west-2", azure_sub_id: str = None):
        self.region = region
        self.azure_sub_id = azure_sub_id
        # We may need one S3 client per region, so we keep them here and reuse them.
        self.s3_clients = {}

    # ==================================================================
    # AWS
    # ==================================================================

    def _get_s3_client(self, region: str):
        """Returns an S3 client for the given region, creating it only once."""
        import boto3
        if region not in self.s3_clients:
            self.s3_clients[region] = boto3.client("s3", region_name=region)
        return self.s3_clients[region]

    @staticmethod
    def _resolve_bucket_region(client, bucket_name: str, fallback: str) -> str:
        """Asks AWS which region a bucket is actually in."""
        try:
            answer = client.get_bucket_location(Bucket=bucket_name)
        except Exception as e:
            logger.warning(f"Could not find the region for bucket '{bucket_name}': {e}")
            return fallback

        region = answer.get("LocationConstraint")

        # AWS returns nothing at all for buckets in us-east-1.
        if region is None:
            return "us-east-1"

        # Translate the old "EU" name if we see it.
        return OLD_REGION_NAMES.get(region, region)

    @staticmethod
    def _read_bucket_setting(client, method_name: str, bucket_name: str):
        """
        Calls one get_bucket_* method and returns its answer, or None.

        The important part is telling the two kinds of failure apart. "This
        bucket has no lifecycle rules" is normal. "Access denied" is not, and
        ignoring it would mean the migration quietly loses a setting that
        really was there.
        """
        try:
            answer = getattr(client, method_name)(Bucket=bucket_name)
            answer.pop("ResponseMetadata", None)
            return answer
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in NOT_CONFIGURED:
                return None
            if type(e).__name__ == "ParamValidationError":
                # This method needs arguments we do not have, because the
                # setting is read per rule id rather than per bucket.
                return None
            logger.warning(f"Could not read {method_name} for '{bucket_name}': {e}")
            return None

    @staticmethod
    def _tagset_to_dict(tag_set: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        Turns AWS's [{"Key": k, "Value": v}, ...] into {k: v}.

        A tag with no key cannot be recreated anywhere, so it is skipped. AWS
        will not return one, but a hand-built fixture might.
        """
        tags = {}
        for tag in tag_set or []:
            key = tag.get("Key")
            if key:
                tags[str(key)] = str(tag.get("Value", ""))
        return tags

    @classmethod
    def _read_route53_tags(cls, client, zone_id: str) -> Dict[str, str]:
        """
        Reads the tags on one hosted zone.

        The id we hold looks like "/hostedzone/Z123456", but this call wants
        the bare id, so the prefix is cut off first. A zone with no tags comes
        back with an empty list rather than an error, so anything raised here
        is a real problem and is logged rather than swallowed.
        """
        bare_id = str(zone_id).rsplit("/", 1)[-1]

        try:
            answer = client.list_tags_for_resource(
                ResourceType="hostedzone", ResourceId=bare_id)
        except Exception as e:
            logger.warning(f"Could not read tags for zone '{zone_id}': {e}")
            return {}

        tag_set = (answer or {}).get("ResourceTagSet", {}).get("Tags", [])
        return cls._tagset_to_dict(tag_set)

    def discover_aws_infrastructure(self) -> List[Dict[str, Any]]:
        """Reads all S3 buckets and Route 53 zones from AWS."""
        import boto3
        logger.info("Reading resources from AWS...")
        found = []

        try:
            first_s3_client = self._get_s3_client(self.region)
            r53_client = boto3.client("route53")
        except Exception as e:
            logger.error(f"Could not connect to AWS: {e}")
            return found

        # ---- S3 buckets ----
        try:
            bucket_list = first_s3_client.list_buckets()

            for bucket in bucket_list.get("Buckets", []):
                try:
                    name = bucket["Name"]

                    # list_buckets gives us every bucket in the account, no matter
                    # which region it is in, so we have to look the region up.
                    region = self._resolve_bucket_region(
                        first_s3_client, name, self.region
                    )

                    # Settings can only be read using a client in the bucket's own
                    # region. Using the wrong region makes AWS reject the request,
                    # and we would end up with a bucket that has no settings at all.
                    client = self._get_s3_client(region)

                    properties = {"bucket": name, "region": region}

                    # Instead of listing every setting by hand, we find all the
                    # methods that begin with "get_bucket_" and call each one.
                    setting_methods = [
                        m for m in dir(client) if m.startswith("get_bucket_")
                    ]

                    for method_name in setting_methods:
                        # "get_bucket_versioning" becomes "versioning"
                        key = method_name.replace("get_bucket_", "")
                        answer = self._read_bucket_setting(client, method_name, name)
                        if answer is not None:
                            properties[key] = answer

                    # get_bucket_tagging is picked up by the loop above, but it
                    # arrives as {"TagSet": [{"Key": k, "Value": v}, ...]}.
                    # Every other cloud and every other resource here uses a
                    # plain dict, so we convert it once, at the edge, and the
                    # rest of the pipeline only ever sees one shape.
                    tagging = properties.pop("tagging", None)
                    if tagging:
                        tags = self._tagset_to_dict(tagging.get("TagSet", []))
                        if tags:
                            properties["tags"] = tags

                    found.append({
                        "type": "aws_s3_bucket",
                        "id": name,
                        "properties": properties,
                    })

                except Exception as e:
                    logger.warning(f"Skipped bucket '{bucket.get('Name')}': {e}")

        except Exception as e:
            logger.error(f"Could not read S3 buckets: {e}")

        # ---- Route 53 zones ----
        try:
            # Route 53 only sends back 100 zones and 300 records at a time.
            # A paginator asks for the next page automatically until there
            # are none left, so we do not miss anything.
            zone_pages = r53_client.get_paginator("list_hosted_zones")

            for zone_page in zone_pages.paginate():
                for zone in zone_page.get("HostedZones", []):
                    try:
                        zone_id = zone["Id"]
                        zone_name = zone["Name"].rstrip(".")
                        is_private = zone.get("Config", {}).get("PrivateZone", False)

                        if is_private:
                            logger.warning(
                                f"Zone '{zone_name}' is private. Azure needs a "
                                "private DNS zone and a VNet link, so this will "
                                "become a public zone unless it is fixed by hand."
                            )

                        records = []
                        record_pages = r53_client.get_paginator(
                            "list_resource_record_sets"
                        )

                        for record_page in record_pages.paginate(HostedZoneId=zone_id):
                            for record in record_page.get("ResourceRecordSets", []):
                                records.append(self._extract_route53_record(record))

                        properties = {"name": zone_name, "records": records}

                        # Zone tags are not part of the zone object. They are
                        # held against the zone as a separate resource and need
                        # a call of their own.
                        tags = self._read_route53_tags(r53_client, zone_id)
                        if tags:
                            properties["tags"] = tags

                        # Only added when it is true. mapper.py reports any key it
                        # does not recognise as a problem, so adding it every time
                        # would fill the report with warnings about normal zones.
                        if is_private:
                            properties["is_private"] = True

                        found.append({
                            "type": "aws_route53_zone",
                            "id": zone_id,
                            "properties": properties,
                        })

                    except Exception as e:
                        logger.warning(f"Skipped zone '{zone.get('Name')}': {e}")

        except Exception as e:
            logger.error(f"Could not read Route 53 zones: {e}")

        logger.info(f"Found {len(found)} AWS resource(s).")
        return found

    @staticmethod
    def _extract_route53_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """Turns one Route 53 record into the simple format we use everywhere."""

        # A record is "Simple" unless AWS added one of these extra keys to it.
        # Anything that is not Simple cannot be copied to Azure directly, so we
        # label it here and mapper.py writes it into the gap report.
        if "Failover" in record:
            policy = "Failover"
        elif "Weight" in record:
            policy = "Weighted"
        elif "Region" in record:
            policy = "Latency"
        elif "GeoProximityLocation" in record:
            policy = "Geoproximity"
        elif "GeoLocation" in record:
            policy = "Geolocation"
        elif "MultiValueAnswer" in record:
            policy = "MultiValue"
        elif "CidrRoutingConfig" in record:
            policy = "CidrRouting"
        elif "TrafficPolicyInstanceId" in record:
            policy = "TrafficPolicy"
        else:
            policy = "Simple"

        record_type = record["Type"]

        # An alias record points at another AWS service such as a load balancer.
        # Azure has nothing like it, so we mark it and warn the user.
        is_alias = bool(record.get("AliasTarget"))

        if is_alias:
            target = record["AliasTarget"]["DNSName"]
            values = [target]
            logger.warning(
                f"Record '{record['Name']}' is an AWS alias pointing at "
                f"'{target}'. Azure has no equivalent, so check this by hand."
            )
        else:
            values = [item["Value"] for item in record.get("ResourceRecords", [])]

            # A long TXT value arrives as several quoted pieces: "part1" "part2".
            # We join the pieces so the value is one complete string again.
            if record_type in ("TXT", "SPF"):
                joined_values = []
                for value in values:
                    pieces = QUOTED_PIECE.findall(value)
                    joined_values.append("".join(pieces) if pieces else value)
                values = joined_values

        return {
            "name": record["Name"],
            "type": record_type,
            "ttl": record.get("TTL", 300),
            "values": values,
            "routing_policy": policy,
            "is_alias": is_alias,
        }

    # ==================================================================
    # Azure
    # ==================================================================

    @staticmethod
    def _resource_group_of(resource_id: str) -> str:
        """
        Pulls the resource group out of an Azure resource id.

        Ids look like the line below, and nearly every read call wants the
        group name as its own argument, so we have to take it back out again:
            /subscriptions/<sub>/resourceGroups/<group>/providers/...
        """
        match = re.search(r"/resourceGroups/([^/]+)", resource_id or "",
                          flags=re.IGNORECASE)
        return match.group(1) if match else ""

    def discover_azure_infrastructure(self) -> List[Dict[str, Any]]:
        """Reads all storage accounts and public DNS zones from Azure."""
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.dns import DnsManagementClient
        from azure.mgmt.storage import StorageManagementClient

        if not self.azure_sub_id:
            raise ValueError("An Azure subscription ID is required to read from Azure.")

        logger.info("Reading resources from Azure...")
        found = []

        try:
            # DefaultAzureCredential tries the environment, then the Azure CLI
            # login, then a managed identity, so the same code works locally
            # and on a build agent.
            credential = DefaultAzureCredential()
            storage_client = StorageManagementClient(credential, self.azure_sub_id)
            dns_client = DnsManagementClient(credential, self.azure_sub_id)
        except Exception as e:
            logger.error(f"Could not connect to Azure: {e}")
            return found

        found.extend(self._discover_azure_storage(storage_client))
        found.extend(self._discover_azure_dns(dns_client))

        logger.info(f"Found {len(found)} Azure resource(s).")
        return found

    # ------------------------------------------------------------------
    # Azure storage accounts
    # ------------------------------------------------------------------

    def _discover_azure_storage(self, client) -> List[Dict[str, Any]]:
        resources = []

        try:
            # The SDK pages this for us; iterating walks every page.
            accounts = list(client.storage_accounts.list())
        except Exception as e:
            logger.error(f"Could not list storage accounts: {e}")
            return resources

        for account in accounts:
            try:
                group = self._resource_group_of(getattr(account, "id", ""))

                properties = {
                    "name": account.name,
                    "location": account.location,
                }

                # Tags sit directly on the account object, unlike the AWS side
                # where they need a call of their own.
                tags = getattr(account, "tags", None)
                if tags:
                    properties["tags"] = {str(k): str(v) for k, v in dict(tags).items()}

                # The SKU holds two separate ideas in one string: "Standard_LRS"
                # means tier Standard with LRS replication.
                sku_name = getattr(getattr(account, "sku", None), "name", "") or ""
                if "_" in sku_name:
                    tier, replication = sku_name.split("_", 1)
                    properties["account_tier"] = tier
                    properties["account_replication_type"] = replication

                # Versioning lives on the blob service rather than on the
                # account itself, so it takes a second call.
                blob_properties = self._read_blob_properties(client, group, account.name)
                if blob_properties:
                    properties["blob_service_properties"] = blob_properties

                containers = self._read_containers(client, group, account.name)
                if containers:
                    properties["containers"] = containers

                # Lifecycle rules are a separate object again. S3 has its own
                # lifecycle rules but we do not translate them yet, so this is
                # carried through only so it appears in the gap report.
                lifecycle = self._read_management_policy(client, group, account.name)
                if lifecycle:
                    properties["lifecycle_management_policy"] = lifecycle

                resources.append({
                    "type": "azurerm_storage_account",
                    "id": account.name,
                    "properties": properties,
                })

            except Exception as e:
                logger.warning(f"Skipped storage account "
                               f"'{getattr(account, 'name', '?')}': {e}")

        return resources

    @staticmethod
    def _read_blob_properties(client, group: str, account_name: str) -> Dict[str, Any]:
        """Reads the blob service settings for one storage account."""
        try:
            blob = client.blob_services.get_service_properties(
                group, account_name, "default")
        except Exception as e:
            logger.warning(f"Could not read blob settings for '{account_name}': {e}")
            return {}

        # Only plain values are kept, so the rest of the pipeline never has to
        # touch an SDK object. registry.py reads is_versioning_enabled; the
        # other two are here because losing them silently would be worse than
        # seeing them in the gap report.
        settings = {
            "is_versioning_enabled": bool(getattr(blob, "is_versioning_enabled", False))
        }

        delete_policy = getattr(blob, "delete_retention_policy", None)
        if delete_policy is not None and getattr(delete_policy, "enabled", False):
            settings["delete_retention_days"] = getattr(delete_policy, "days", None)

        change_feed = getattr(blob, "change_feed", None)
        if change_feed is not None and getattr(change_feed, "enabled", False):
            settings["change_feed_enabled"] = True

        return settings

    @staticmethod
    def _read_containers(client, group: str, account_name: str) -> List[Dict[str, Any]]:
        """
        Lists the containers in one storage account.

        S3 has nothing that matches a container, so these are only reported,
        never created. Public access is included because a container that was
        readable by anyone is worth seeing in the report.
        """
        try:
            return [
                {"name": container.name,
                 "public_access": str(getattr(container, "public_access", None))}
                for container in client.blob_containers.list(group, account_name)
            ]
        except Exception as e:
            logger.warning(f"Could not list containers for '{account_name}': {e}")
            return []

    @staticmethod
    def _read_management_policy(client, group: str, account_name: str) -> List[Any]:
        """Reads the lifecycle rules, if this account has any."""
        try:
            policy = client.management_policies.get(group, account_name, "default")
        except Exception:
            # An account with no lifecycle rules raises ResourceNotFound, which
            # is completely normal and not worth a warning.
            return []

        rules = getattr(getattr(policy, "policy", None), "rules", None) or []
        return [rule.as_dict() if hasattr(rule, "as_dict") else str(rule)
                for rule in rules]

    # ------------------------------------------------------------------
    # Azure DNS
    # ------------------------------------------------------------------

    def _discover_azure_dns(self, client) -> List[Dict[str, Any]]:
        resources = []

        try:
            zones = list(client.zones.list())
        except Exception as e:
            logger.error(f"Could not list DNS zones: {e}")
            return resources

        for zone in zones:
            try:
                group = self._resource_group_of(getattr(zone, "id", ""))

                # This client only reads public zones. Private zones live in a
                # different service (azure.mgmt.privatedns) and would need
                # their own pass, so we say so instead of pretending.
                if str(getattr(zone, "zone_type", "Public")).lower().endswith("private"):
                    logger.warning(f"Zone '{zone.name}' is private and was skipped. "
                                   "Private zones need azure.mgmt.privatedns.")
                    continue

                records = []
                for record_set in client.record_sets.list_by_dns_zone(group, zone.name):
                    record = self._extract_azure_record(record_set)
                    if record is not None:
                        records.append(record)

                properties = {
                    "name": zone.name,
                    "resource_group_name": group,
                    "records": records,
                }

                # Only added when there are some, for the same reason as
                # is_private on the AWS side: an empty value is only noise.
                tags = getattr(zone, "tags", None)
                if tags:
                    properties["tags"] = {str(k): str(v)
                                          for k, v in dict(tags).items()}

                resources.append({
                    "type": "azurerm_dns_zone",
                    "id": zone.name,
                    "properties": properties,
                })

            except Exception as e:
                logger.warning(f"Skipped DNS zone '{getattr(zone, 'name', '?')}': {e}")

        return resources

    @staticmethod
    def _extract_azure_record(record_set) -> Dict[str, Any]:
        """
        Turns one Azure record set into the same shape as an AWS record.

        Azure keeps each record type in its own field and splits the values
        into named parts. AWS keeps one flat string per value. We join the
        parts back together here so that mapper.py and generator.py only ever
        deal with a single format.
        """
        # The type arrives as "Microsoft.Network/dnszones/A".
        record_type = str(record_set.type).split("/")[-1].upper()

        # An Azure alias record set points at another Azure resource and has no
        # values of its own, exactly like an AWS alias record.
        target = getattr(record_set, "target_resource", None)
        if target is not None and getattr(target, "id", None):
            logger.warning(f"Record '{record_set.name}' is an Azure alias record "
                           "pointing at another Azure resource. AWS has no direct "
                           "equivalent, so check this by hand.")
            return {
                "name": record_set.name,
                "type": record_type,
                "ttl": getattr(record_set, "ttl", 300) or 300,
                "values": [target.id],
                "is_alias": True,
            }

        values = []

        if record_type == "A":
            values = [r.ipv4_address for r in record_set.a_records or []]

        elif record_type == "AAAA":
            values = [r.ipv6_address for r in record_set.aaaa_records or []]

        elif record_type == "CNAME":
            # Singular field: a CNAME can only point at one place.
            cname = getattr(record_set, "cname_record", None)
            values = [cname.cname] if cname else []

        elif record_type == "MX":
            values = [f"{r.preference} {r.exchange}"
                      for r in record_set.mx_records or []]

        elif record_type == "NS":
            values = [r.nsdname for r in record_set.ns_records or []]

        elif record_type == "PTR":
            values = [r.ptrdname for r in record_set.ptr_records or []]

        elif record_type == "SRV":
            values = [f"{r.priority} {r.weight} {r.port} {r.target}"
                      for r in record_set.srv_records or []]

        elif record_type == "CAA":
            # AWS writes the value part in quotes, so we match that here.
            values = [f'{r.flags} {r.tag} "{r.value}"'
                      for r in record_set.caa_records or []]

        elif record_type == "TXT":
            # Azure stores a long TXT value as a list of 255 character pieces.
            # Joining them gives the one real value back, and generator.py
            # splits it again if the target cloud needs that.
            for r in record_set.txt_records or []:
                pieces = r.value or []
                if isinstance(pieces, list):
                    values.append("".join(pieces))
                else:
                    values.append(str(pieces))

        elif record_type == "SOA":
            # mapper.py drops SOA records because the target cloud makes its
            # own. It is still returned so that the drop is recorded.
            soa = getattr(record_set, "soa_record", None)
            values = [f"{soa.host} {soa.email}"] if soa else []

        else:
            logger.warning(f"Record '{record_set.name}' has an unexpected type "
                           f"'{record_type}' and was skipped.")
            return None

        return {
            "name": record_set.name,
            "type": record_type,
            "ttl": getattr(record_set, "ttl", 300) or 300,
            "values": values,
            "is_alias": False,
        }