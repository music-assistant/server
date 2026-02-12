#!/usr/bin/env python3
"""Synchronize Kion Music provider from Yandex Music provider.

Applies intelligent transformations to sync code while preserving
Kion-specific customizations (API endpoint, branding).

Usage:
    python scripts/sync_kion_from_yandex.py
    python scripts/sync_kion_from_yandex.py --dry-run
    python scripts/sync_kion_from_yandex.py --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
PROVIDERS_DIR = PROJECT_ROOT / "music_assistant" / "providers"
YANDEX_DIR = PROVIDERS_DIR / "yandex_music"
KION_DIR = PROVIDERS_DIR / "kion_music"
DEFAULT_CONFIG = PROJECT_ROOT / ".github" / "sync-config.yml"


class ProviderSynchronizer:
    """Synchronizes Kion Music provider from Yandex Music provider."""

    def __init__(self, config_path: Path) -> None:
        """Initialize synchronizer with configuration.

        :param config_path: Path to sync configuration YAML file.
        """
        self.config = self._load_config(config_path)
        self.stats = {"files_synced": 0, "transformations_applied": 0, "errors": 0}

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        """Load synchronization configuration from YAML.

        :param config_path: Path to configuration file.
        """
        if not config_path.exists():
            msg = f"Configuration file not found: {config_path}"
            raise FileNotFoundError(msg)

        with config_path.open() as f:
            return yaml.safe_load(f)

    def _apply_transformations(self, content: str, filename: str) -> str:
        """Apply global and file-specific transformation rules.

        :param content: File content to transform.
        :param filename: Name of file being transformed.
        """
        result = content

        # Apply global transformations
        for transform in self.config.get("transformations", []):
            pattern = transform["pattern"]
            replacement = transform["replacement"]
            before = result
            result = result.replace(pattern, replacement)
            if result != before:
                self.stats["transformations_applied"] += 1
                logger.debug(f"Applied global transformation: '{pattern}' -> '{replacement}'")

        # Apply file-specific transformations
        file_transforms = self.config.get("file_transformations", {}).get(filename, [])
        for transform in file_transforms:
            pattern = transform["pattern"]
            replacement = transform["replacement"]
            before = result
            result = result.replace(pattern, replacement)
            if result != before:
                self.stats["transformations_applied"] += 1
                logger.debug(
                    f"Applied file-specific transformation in {filename}: "
                    f"'{pattern}' -> '{replacement}'"
                )

        return result

    def sync_file(self, filename: str, *, dry_run: bool = False) -> bool:
        """Sync a single file from Yandex to Kion.

        :param filename: Name of file to sync.
        :param dry_run: If True, don't write changes to disk.
        """
        yandex_file = YANDEX_DIR / filename
        kion_file = KION_DIR / filename

        if not yandex_file.exists():
            logger.warning(f"Source file not found: {yandex_file}")
            return False

        logger.info(f"Syncing {filename}...")

        try:
            # Read Yandex file
            content = yandex_file.read_text(encoding="utf-8")

            # Apply transformations
            transformed = self._apply_transformations(content, filename)

            # Write to Kion file (if not dry run)
            if not dry_run:
                kion_file.parent.mkdir(parents=True, exist_ok=True)
                kion_file.write_text(transformed, encoding="utf-8")
                logger.info(f"  ✓ Synced {filename}")
            else:
                logger.info(f"  [DRY RUN] Would sync {filename}")

            self.stats["files_synced"] += 1
            return True

        except Exception:
            logger.exception(f"  ✗ Error syncing {filename}")
            self.stats["errors"] += 1
            return False

    def sync_all(self, *, dry_run: bool = False) -> bool:
        """Sync all configured files.

        :param dry_run: If True, don't write changes to disk.
        """
        logger.info("Starting provider synchronization...")
        logger.info(f"Upstream: {self.config['upstream_provider']}")
        logger.info(f"Downstream: {self.config['downstream_provider']}")
        logger.info("")

        sync_files = self.config.get("sync_files", [])
        for filename in sync_files:
            self.sync_file(filename, dry_run=dry_run)

        # Print summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("Synchronization Summary")
        logger.info("=" * 60)
        logger.info(f"Files synced: {self.stats['files_synced']}/{len(sync_files)}")
        logger.info(f"Transformations: {self.stats['transformations_applied']}")
        logger.info(f"Errors: {self.stats['errors']}")

        return self.stats["errors"] == 0


def main() -> None:
    """Run the synchronization script."""
    parser = argparse.ArgumentParser(
        description="Sync Kion Music provider from Yandex Music provider"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to sync configuration YAML (default: .github/sync-config.yml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without writing changes",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        synchronizer = ProviderSynchronizer(args.config)
        success = synchronizer.sync_all(dry_run=args.dry_run)
        sys.exit(0 if success else 1)
    except Exception:
        logger.exception("Synchronization failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
