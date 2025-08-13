"""Fixture file path to type mapping utilities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from tests.providers.nicovideo.constants import GENERATED_DIR
from tests.providers.nicovideo.types import FixtureAPIResult

logger = logging.getLogger(__name__)


@dataclass
class FixturePathToTypeMapping:
    """Class for managing fixture type information."""

    key: str  # "tracks/user_history"
    fixture_type: type[BaseModel] | None = None

    @property
    def category(self) -> str:
        """Get category name."""
        return self.key.split("/", 1)[0]

    @property
    def filename(self) -> str:
        """Get filename."""
        return self.key.split("/", 1)[1]

    def auto_detect_fixture_type[T: BaseModel](self, response: FixtureAPIResult[T]) -> None:
        """Automatically detect fixture type from API response."""
        # For list case
        if isinstance(response, list):
            if response:  # Only if list is not empty
                first_item = response[0]
                item_type = type(first_item)
                if issubclass(item_type, BaseModel):
                    logger.info(f"Auto-detected list type with items: {item_type.__name__}")
                    self.fixture_type = item_type
            return

        # For single object case
        self.fixture_type = type(response)
        module_name = self.fixture_type.__module__
        logger.info(f"Auto-detected type: {self.fixture_type.__name__} from {module_name}")
        return


class PathToTypeMapper:
    """Maps fixture file paths to their corresponding API response types."""

    def __init__(self) -> None:
        """Initialize the path to type mapping."""
        self.fixture_mappings: dict[str, FixturePathToTypeMapping] = {}

    def record_type_mapping[T: BaseModel](
        self, response: FixtureAPIResult[T], category: str, name: str
    ) -> FixturePathToTypeMapping:
        """Record type mapping for automatic generation."""
        key = f"{category}/{name}.json"

        mapping = FixturePathToTypeMapping(key=key, fixture_type=None)
        mapping.auto_detect_fixture_type(response)
        self.fixture_mappings[key] = mapping
        return mapping

    def collect_required_imports(self) -> set[tuple[str, str]]:
        """Collect all required imports from fixture mappings."""
        needed_imports = set()

        for mapping in self.fixture_mappings.values():
            # Collect import information for fixture_type
            if (
                mapping.fixture_type
                and isinstance(mapping.fixture_type, type)
                and mapping.fixture_type.__module__.startswith("niconico.objects")
            ):
                needed_imports.add((mapping.fixture_type.__module__, mapping.fixture_type.__name__))

        return needed_imports

    def write_imports_section(self, f: TextIO, needed_imports: set[tuple[str, str]]) -> None:
        """Write the imports section of the fixture_types.py file."""
        # Group imports by module
        imports_by_module: dict[str, list[str]] = {}
        for module, name in needed_imports:
            if module not in imports_by_module:
                imports_by_module[module] = []
            imports_by_module[module].append(name)

        # Write imports
        if imports_by_module:
            f.write("# Import all necessary types for fixture mapping\n")
            for module, names in sorted(imports_by_module.items()):
                if module == "niconico.objects.nvapi":
                    f.write("from niconico.objects.nvapi import (\n")
                    for name in sorted(names):
                        f.write(f"    {name},\n")
                    f.write(")\n")
                else:
                    f.write(f"from {module} import {', '.join(sorted(names))}\n")

    def generate_fixture_type_mapping(self, f: TextIO) -> None:
        """Generate simple path to type mapping variable."""
        f.write("# Fixture type mappings: path -> type\n")
        f.write("FIXTURE_TYPE_MAPPINGS: dict[str, type[BaseModel]] = {\n")

        for key, mapping in sorted(self.fixture_mappings.items()):
            if mapping.fixture_type is not None:
                type_name = mapping.fixture_type.__name__
                f.write(f'    "{key}": {type_name},\n')

        f.write("}\n")

    def _generate_init_file(self, generated_dir: Path) -> None:
        """Generate __init__.py file for the generated fixtures package."""
        init_path = generated_dir / "__init__.py"

        with init_path.open("w", encoding="utf-8") as f:
            f.write('"""Generated fixtures for testing."""\n\n')
            f.write("from .fixture_types import FIXTURE_TYPE_MAPPINGS\n\n")
            f.write("__all__ = [\n")
            f.write('    "FIXTURE_TYPE_MAPPINGS",\n')
            f.write("]\n")

    def generate_fixture_types_file(self) -> None:
        """Generate fixture_types.py file from collected type mappings."""
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)

        fixture_types_path = GENERATED_DIR / "fixture_types.py"

        # Collect all required imports
        needed_imports = self.collect_required_imports()

        with fixture_types_path.open("w", encoding="utf-8") as f:
            f.write('"""Fixture type mappings for automatic deserialization."""\n\n')
            f.write("from __future__ import annotations\n\n")
            f.write("from typing import TYPE_CHECKING\n\n")

            # Write imports
            self.write_imports_section(f, needed_imports)
            f.write("\n")
            f.write("if TYPE_CHECKING:\n")
            f.write("    from pydantic import BaseModel\n")
            f.write("\n")

            # Generate simple type mapping
            self.generate_fixture_type_mapping(f)

        # Generate __init__.py file
        self._generate_init_file(GENERATED_DIR)

        logger.info(f"Generated path->type mapping at {fixture_types_path}")
        logger.info(f"Generated __init__ at {GENERATED_DIR / '__init__.py'}")
