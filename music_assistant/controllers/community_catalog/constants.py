"""Constants of the community catalog."""

from __future__ import annotations

from typing import Final

# not "enabled": saving a config takes that key for the enabled flag of a provider config
CONF_ENABLED: Final = "catalog_enabled"
# the folder in the data directory that holds what the server keeps of the community catalog
COMMUNITY_PROVIDERS_FOLDER: Final = "community_providers"
# the versions of the index format and of the state file this server reads
INDEX_SCHEMA: Final = 1
STATE_SCHEMA: Final = 1
