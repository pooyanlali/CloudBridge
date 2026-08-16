"""
core/registry.py
Declarative transformation rules matching schema endpoints between cloud providers.
"""
# Primary mapping: Azure -> AWS (Targeting closest physical location and lowest latency)
AZURE_TO_AWS_REGION_MAP = {

    # North America
    "eastus": "us-east-1",
    "eastus2": "us-east-1",
    "centralus": "us-east-2",
    "northcentralus": "us-east-2",
    "southcentralus": "us-east-2",
    "westus": "us-west-1",
    "westus2": "us-west-2",
    "westus3": "us-west-1",
    "westcentralus": "us-west-2",
    "canadacentral": "ca-central-1",
    "canadaeast": "ca-central-1",
    
    # Europe
    "northeurope": "eu-west-1",
    "westeurope": "eu-central-1",
    "uksouth": "eu-west-2",
    "ukwest": "eu-west-2",
    "francecentral": "eu-west-3",
    "francesouth": "eu-west-3",
    "germanywestcentral": "eu-central-1",
    "germanynorth": "eu-central-1",
    "switzerlandnorth": "eu-central-2",
    "switzerlandwest": "eu-central-2",
    "swedencentral": "eu-north-1",
    "swedensouth": "eu-north-1",
    "norwayeast": "eu-north-1",
    "norwaywest": "eu-north-1",
    "italynorth": "eu-south-1",
    "spaincentral": "eu-south-2",

    # Asia Pacific
    "southeastasia": "ap-southeast-1",
    "eastasia": "ap-east-1",
    "australiaeast": "ap-southeast-2",
    "australiasoutheast": "ap-southeast-4",
    "australiacentral": "ap-southeast-2",
    "australiacentral2": "ap-southeast-2",
    "japaneast": "ap-northeast-1",
    "japanwest": "ap-northeast-3",
    "koreacentral": "ap-northeast-2",
    "koreasouth": "ap-northeast-2",
    "centralindia": "ap-south-1",
    "southindia": "ap-south-2",
    "westindia": "ap-south-1",
    "indonesiacentral": "ap-southeast-3", 

    # Middle East & Africa
    "uaenorth": "me-central-1",
    "uaecentral": "me-central-1",
    "israelcentral": "il-central-1",
    "southafricanorth": "af-south-1",
    "southafricawest": "af-south-1",

    # South America
    "brazilsouth": "sa-east-1",
    "brazilsoutheast": "sa-east-1",
    
    # NOTE: "global" is intentionally excluded from this map.
    # Passing it through would emit location = "global" into HCL, which the AzureRM provider rejects at plan time. 
    # Omitting it causes mapper.py to invoke log_regional_fallback() and resolve to a concrete region instead.
}

# Reverse mapping: AWS -> Azure (Targeting primary Azure region in the specified geography)
# Prevents silent key overwriting and ensures workloads map back to tier-1 Azure regions.
AWS_TO_AZURE_REGION_MAP = {
    # North America
    "us-east-1": "eastus",
    "us-east-2": "centralus",
    "us-west-1": "westus",
    "us-west-2": "westus2",
    "ca-central-1": "canadacentral",
    "ca-west-1": "canadacentral", 

    # Europe
    "eu-west-1": "northeurope",
    "eu-west-2": "uksouth",
    "eu-west-3": "francecentral",
    "eu-central-1": "germanywestcentral", 
    "eu-central-2": "switzerlandnorth",
    "eu-north-1": "swedencentral",
    "eu-south-1": "italynorth",
    "eu-south-2": "spaincentral",

    # Asia Pacific
    "ap-southeast-1": "southeastasia",
    "ap-southeast-2": "australiaeast",
    "ap-southeast-3": "indonesiacentral", # 
    "ap-southeast-4": "australiasoutheast",
    "ap-east-1": "eastasia",
    "ap-northeast-1": "japaneast",
    "ap-northeast-2": "koreacentral",
    "ap-northeast-3": "japanwest",
    "ap-south-1": "westindia", 
    "ap-south-2": "southindia",

    # Middle East & Africa
    "me-central-1": "uaecentral",
    "me-south-1": "uaenorth", # Bahrain fallback to UAE (no exact match available)
    "af-south-1": "southafricawest", 
    "il-central-1": "israelcentral",

    # South America
    "sa-east-1": "brazilsouth",

    # NOTE: "global" is intentionally excluded for the same reasons as in AZURE_TO_AWS_REGION_MAP. 
}

MAPPING_REGISTRY = {
    "aws2azure": {
        "aws_s3_bucket": {
            "target_type": "azurerm_storage_account",
            # Mandatory defaults required by the AzureRM Terraform Provider
            "target_defaults": {
                "account_tier": "Standard",
                "account_replication_type": "LRS"
            },
            "attribute_mappings": {
                "bucket": "name",
                "region": "location"
            },
            "structural_mappings": {
                "versioning": {
                    "target_block": "blob_properties",
                    "target_key": "versioning_enabled",
                    # Safely evaluates AWS Versioning payload
                    "value_transformation": lambda v: True if isinstance(v, dict) and v.get('Status') == "Enabled" else False
                }
            },
            # location is dropped because Azure determines location via the Resource Group
            "ignored_attributes": ["location", "encryption"]
        },
        "aws_route53_zone": {
            "target_type": "azurerm_dns_zone",
            "attribute_mappings": {
                "name": "name"
            },
            "structural_mappings": {
                "records": {
                    "target_block": "records_loop",
                    # The handler_directive allows your mapper to send DNS arrays to a dedicated parsing function
                    "handler_directive": "process_dns_records" 
                }
            },
            # "vpc" is ignored at registry level. private hosted zones (those with a VPC association) are silently converted to public Azure DNS zones. 
            # Azure private DNS requires an explicit azurerm_private_dns_zone + VNet link; 
            "ignored_attributes": ["comment", "vpc"]
        }
    },
    "azure2aws": {
        "azurerm_storage_account": {
            "target_type": "aws_s3_bucket",
            "target_defaults": {
                # WARNING: force_destroy allows terraform destroy to delete non-empty buckets
                # without error. Safe for test environments.
                "force_destroy": True
            },
            "attribute_mappings": {
                "name": "bucket",
                "location": "region"
            },
            "structural_mappings": {
                "blob_service_properties": {
                    # Note: HCL generator must convert this to aws_s3_bucket_versioning resource block
                    "target_block": "versioning",
                    "target_key": "status",
                    "value_transformation": lambda v: "Enabled" if isinstance(v, dict) and (v.get("is_versioning_enabled") is True or v.get("isVersioningEnabled") is True) else "Suspended"
                }
            },
            "ignored_attributes": ["account_tier", "account_replication_type", "containers"]
        },
        "azurerm_dns_zone": {
            "target_type": "aws_route53_zone",
            "attribute_mappings": {
                "name": "name"
            },
            "structural_mappings": {
                "records": {
                    "target_block": "records_loop",
                    "handler_directive": "process_dns_records"
                }
            },
            "ignored_attributes": ["resource_group_name", "tags"]
        }
    }
}