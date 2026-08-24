"""
core/deployer.py
Runs the Terraform command line tool against the folder we just generated.

This  runs init, plan or apply and raises an error if Terraform is having problems.
Deciding what to do about that error is migrate.py's job, which is why nothing in here calls
sys.exit(): a library that kills the interpreter cannot be reused or tested.
"""

import logging
import subprocess
from pathlib import Path
from typing import List

logger = logging.getLogger("core.deployer")


class TerraformError(RuntimeError):
    """Raised when Terraform is missing or returns a non-zero exit code."""


class TerraformDeployer:

    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)

    # ------------------------------------------------------------------
    # Running one command
    # ------------------------------------------------------------------

    def _run(self, command: List[str], stream: bool = True) -> None:
        """
        Runs one Terraform command inside the generated workspace.

        When stream is True the output is printed as it arrives, which matters
        for apply because it can take minutes. utf-8 is set explicitly so the
        box-drawing characters Terraform prints do not break on Windows.
        """
        printable = " ".join(command)
        logger.debug(f"Running: {printable} (in {self.work_dir})")

        try:
            if stream:
                process = subprocess.Popen(
                    command,
                    cwd=self.work_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                for line in process.stdout:
                    print(line, end="")
                return_code = process.wait()

                if return_code != 0:
                    raise TerraformError(
                        f"'{printable}' failed with exit code {return_code}. "
                        "The Terraform output above explains why.")
            else:
                result = subprocess.run(
                    command,
                    cwd=self.work_dir,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode != 0:
                    raise TerraformError(
                        f"'{printable}' failed with exit code "
                        f"{result.returncode}.\n{result.stderr.strip()}")

        except FileNotFoundError:
            # Raised when the terraform binary itself is not on the PATH.
            raise TerraformError(
                "Terraform was not found. Install it and make sure it is on "
                "your PATH, or run again with --skip-deploy.")

    # ------------------------------------------------------------------
    # The three commands we use
    # ------------------------------------------------------------------

    def init(self) -> None:
        logger.info("Initialising the Terraform workspace...")
        self._run(["terraform", "init", "-input=false"])

    def plan(self) -> None:
        logger.info("Building the Terraform plan...")
        self._run(["terraform", "plan", "-input=false"])

    def apply(self) -> None:
        logger.info("Applying the configuration to the target cloud...")
        self._run(["terraform", "apply", "-auto-approve", "-input=false"])
