#!/usr/bin/env python3
"""
migrate.py
Command line entry point for the migration engine.

The whole run is four steps, in this order:

    1. discovery  - read what already exists in the source cloud
    2. mapper     - translate it into the target cloud's schema
    3. generator  - write that out as Terraform files
    4. deployer   - optionally run terraform init / plan / apply

Everything each step produces is written into one timestamped folder so that
runs never overwrite each other.

The Azure subscription ID is set once in AZURE_SUBSCRIPTION_ID below, so the
only argument the command line needs is --direction.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from core.discovery import SDKDiscoveryEngine
from core.mapper import SchemaMappingEngine
from core.generator import DeclarativeHCLGenerator
from core.reporter import MigrationAuditReporter
from core.deployer import TerraformDeployer, TerraformError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("migrate")

# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
# The Azure subscription to read from when --direction azure2aws is used.
# Put your own subscription ID here.
AZURE_SUBSCRIPTION_ID = "987d03d5-56b9-456e-a47e-d9c81fe109ea"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bidirectional cloud migration engine (AWS <-> Azure).")
    parser.add_argument("--direction", required=True,
                        choices=["aws2azure", "azure2aws"],
                        help="Which way to migrate.")
    parser.add_argument("--aws-region", default="eu-west-2",
                        help="AWS region to read from or write to.")
    parser.add_argument("--output-dir", default="./migration_output",
                        help="Where to put the generated files.")
    parser.add_argument("--skip-deploy", action="store_true",
                        help="Generate the files but do not run Terraform.")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Apply without asking for confirmation first.")
    return parser.parse_args()


def confirm_apply() -> bool:
    """Asks the user once whether we should really change their cloud."""
    while True:
        answer = input("\nApply these changes to the target cloud? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("Please answer y or n.")


def main() -> int:
    args = parse_args()

    # Step 0: work out the settings we need before touching anything.
    # The subscription ID is fixed at the top of this file.
    azure_sub_id = AZURE_SUBSCRIPTION_ID.strip()
    if args.direction == "azure2aws" and (
            not azure_sub_id ):
        logger.error("AZURE_SUBSCRIPTION_ID at the top of migrate.py is still "
                     "the placeholder. Set it to a real subscription ID before "
                     "migrating from Azure.")
        return 1

    run_dir = Path(args.output_dir) / f"run_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("  CLOUD MIGRATION ENGINE")
    logger.info("=" * 60)
    logger.info(f"Direction: {args.direction}")
    logger.info(f"Output folder: {run_dir}")

    reporter = MigrationAuditReporter(output_dir=run_dir)

    try:
        # Step 1: read the source cloud.
        discovery = SDKDiscoveryEngine(region=args.aws_region,
                                       azure_sub_id=azure_sub_id)
        if args.direction == "aws2azure":
            raw_resources = discovery.discover_aws_infrastructure()
        else:
            raw_resources = discovery.discover_azure_infrastructure()

        if not raw_resources:
            logger.warning("Nothing was found in the source cloud. "
                           "Check your credentials and region.")

        # Step 2: translate it.
        mapper = SchemaMappingEngine(direction=args.direction, reporter=reporter)
        target_config = mapper.translate(raw_resources)

        # The translated form is written out as well, because it is much
        # easier to debug than the generated HCL.
        ir_path = run_dir / "canonical_ir.json"
        with open(ir_path, "w", encoding="utf-8") as f:
            json.dump(target_config, f, indent=2, default=str)
        logger.info(f"Translated resources written to {ir_path}")

        # Step 3: write the Terraform files.
        generator = DeclarativeHCLGenerator(output_dir=run_dir,
                                            direction=args.direction,
                                            reporter=reporter)
        written = generator.generate(target_config)
        logger.info(f"Terraform written: {', '.join(p.name for p in written)}")

    except Exception as error:
        logger.error(f"The migration failed: {error}", exc_info=True)
        reporter.write_reports()
        return 1

    # The report is written whatever happens next, so the user always has it
    # even if they decide not to deploy.
    reporter.write_reports()

    if reporter.count("High") > 0:
        logger.warning(f"{reporter.count('High')} high severity gap(s) were "
                       "found. Read the gap report before applying.")

    # Step 4: optionally run Terraform.
    if args.skip_deploy:
        logger.info("Skipping Terraform because --skip-deploy was given.")
        return 0

    try:
        deployer = TerraformDeployer(work_dir=run_dir)
        deployer.init()
        deployer.plan()

        if not args.auto_approve and not confirm_apply():
            logger.info("Not applying. The generated files are still in "
                        f"{run_dir}.")
            return 0

        deployer.apply()
        logger.info("Applied successfully.")
        return 0

    except TerraformError as error:
        logger.error(str(error))
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted. Nothing further was applied.")
        return 1


if __name__ == "__main__":
    sys.exit(main())