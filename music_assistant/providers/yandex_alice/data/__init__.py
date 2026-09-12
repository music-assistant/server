"""
Package-data resources bundled with the Yandex Alice provider.

Holds the declarative skill manifest (``skill.toml``) accessed via
``importlib.resources``. The empty ``__init__.py`` makes
``provider.data`` a real subpackage so ``setuptools.find_packages``
discovers it and the configured
``[tool.setuptools.package-data] "provider.data" = ["*.toml"]`` glob
attaches the TOML to the wheel.
"""
