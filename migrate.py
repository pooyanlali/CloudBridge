"""
migrate.py
Command line entry point for the migration engine.

A run is three steps, in this order:

    1. discovery  - read what already exists in the source cloud
    2. mapper     - translate it into the target cloud's schema, using the rules held as plain data in registry.py
    3. reporter   - write out everything that could not be translated cleanly

Nothing here changes the source cloud or the target cloud. The run only reads,
translates, and reports, so it is safe to run against a live account.

Two files are produced. canonical_ir.json is the translated form of everything
that was found, and migration_gap_report.json lists every difference between
the two clouds that could not be resolved automatically. Both are written into
one timestamped folder so that repeated runs never overwrite each other.

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
from core.reporter import MigrationAuditReporter

# Logging is configured once, here, rather than in each module. Every module
# asks for its own named logger, so each line says which part of the pipeline
# produced it, which matters when discovery and mapper both have something to
# say about the same resource. Sending it to stdout puts it in the same stream
# as the summary printed at the end, so piping the run to a file captures both.
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
# It is held here as a constant so that it appears once.
AZURE_SUBSCRIPTION_ID = "Place your Azure Subscription ID here."


def parse_args():
    """
    Defines the command line.

    Only --direction has to be given. The other two have sensible defaults, so
    a normal run is a single argument.
    """
    parser = argparse.ArgumentParser(
        description="Bidirectional cloud migration engine (AWS <-> Azure).")
    parser.add_argument("--direction", required=True,
                        choices=["aws2azure", "azure2aws"],
                        help="Which way to migrate.")
    parser.add_argument("--aws-region", default="eu-west-2",
                        help="AWS region to read from.")
    parser.add_argument("--output-dir", default="./migration_output",
                        help="Where to put the results of the run.")
    return parser.parse_args()


def print_results(target_config, reporter) -> None:
    """
    Prints what the run found, once everything else has finished.

    The gap report is already on disk as JSON, which is the machine readable
    copy. This is the human readable one: the same information, in the order it
    was recorded, so the run can be understood without opening a file.

    Two lines are used per gap rather than one. The identifier and the note are
    both long enough that putting them together would either wrap
    unpredictably or have to be cut short, and the note is the part that
    explains what was actually lost.
    """
    print()
    print(f"Found {len(target_config['storage_accounts'])} storage account(s) "
          f"and {len(target_config['dns_zones'])} DNS zone(s).")

    if not reporter.gaps:
        print("No gaps: everything translated cleanly.")
        return

    print(f"{len(reporter.gaps)} gap(s) found "
          f"({reporter.count('High')} high, {reporter.count('Medium')} medium, "
          f"{reporter.count('Low')} low):")
    print()

    for gap in reporter.gaps:
        print(f"[{gap['severity']}] {gap['resource_id']} - {gap['attribute']}")
        print(f"    {gap['notes']}")


def main() -> int:
    args = parse_args()

    # ------------------------------------------------------------------
    # Step 0: check the settings before anything is read
    # ------------------------------------------------------------------
    # Reading from Azure needs a subscription ID. Checking it now means a
    # forgotten constant is caught immediately, rather than after discovery has
    # already spent time talking to the cloud.
    azure_sub_id = AZURE_SUBSCRIPTION_ID.strip()
    if args.direction == "azure2aws" and (
            not azure_sub_id):
        logger.error("AZURE_SUBSCRIPTION_ID at the top of migrate.py is invalid")
        return 1

    # One folder per run, named after the time it started. Runs are meant to be
    # compared with one another, so an earlier run must never be overwritten.
    run_dir = Path(args.output_dir) / f"run_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("  CLOUD MIGRATION ENGINE")
    logger.info("=" * 60)
    logger.info(f"Direction: {args.direction}")
    logger.info(f"Output folder: {run_dir}")

    # The reporter is created before the work starts and then passed into the
    # mapper, so that one reporter collects every gap found anywhere in the run
    # and the whole run ends up in a single report.
    reporter = MigrationAuditReporter(output_dir=run_dir)

    try:
        # --------------------------------------------------------------
        # Step 1: read the source cloud
        # --------------------------------------------------------------
        # Which cloud is the source depends entirely on the direction. Both
        # methods return the same shape, so nothing after this point has to
        # know which of the two ran.
        discovery = SDKDiscoveryEngine(region=args.aws_region,
                                       azure_sub_id=azure_sub_id)
        if args.direction == "aws2azure":
            raw_resources = discovery.discover_aws_infrastructure()
        else:
            raw_resources = discovery.discover_azure_infrastructure()

        # An empty result is not an error: the account really might be empty.
        # In practice it is far more often the wrong region or the wrong
        # credentials, so it is worth saying so plainly.
        if not raw_resources:
            logger.warning("Nothing was found in the source cloud. "
                           "Check your credentials and region.")

        # --------------------------------------------------------------
        # Step 2: translate what was found
        # --------------------------------------------------------------
        # The mapper reads its rules from registry.py. Anything those rules do
        # not cover is handed to the reporter rather than dropped quietly,
        # which is why the reporter is passed in here.
        mapper = SchemaMappingEngine(direction=args.direction, reporter=reporter)
        target_config = mapper.translate(raw_resources)

        # The translated form is written out as its own file. It is the exact
        # result of the translation, so it shows what the rules in registry.py
        # actually did, separately from what could not be translated at all.
        ir_path = run_dir / "canonical_ir.json"
        with open(ir_path, "w", encoding="utf-8") as f:
            json.dump(target_config, f, indent=2, default=str)
        logger.info(f"Translated resources written to {ir_path}")

    except Exception as error:
        # Anything unexpected still produces a report. Whatever was found
        # before the failure is more useful than an empty folder, and the
        # traceback is kept so the cause can be traced afterwards.
        logger.error(f"The run failed: {error}", exc_info=True)
        reporter.write_reports()
        return 1

    # ------------------------------------------------------------------
    # Step 3: write and show the report
    # ------------------------------------------------------------------
    report_path = reporter.write_reports()
    print_results(target_config, reporter)

    print()
    print(f"Full report: {report_path}")

    # A high severity gap means something was lost that changes how the
    # infrastructure behaves, so it is repeated here as a warning. It is not
    # treated as a failure: the run did exactly what it was asked to do, and
    # the exit code should say so.
    if reporter.count("High") > 0:
        logger.warning(f"{reporter.count('High')} high severity gap(s) were "
                       "found. These need attention before the translated "
                       "configuration could be used.")

    return 0


if __name__ == "__main__":
    sys.exit(main())