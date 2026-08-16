"""Tests for the license checks of the package safety script."""

from __future__ import annotations

from typing import Any

import pytest

from scripts import check_package_safety
from scripts.check_package_safety import (
    check_license_compatibility,
    check_package,
    get_package_license,
)


def pypi_info(**overrides: Any) -> dict[str, Any]:
    """
    Return the `info` section of a PyPI JSON response.

    :param overrides: Fields to set on top of the (empty) license metadata.
    """
    return {"license": None, "license_expression": None, "classifiers": [], **overrides}


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        # PEP 639: the SPDX expression is the only license metadata (chardet 7.6.0)
        (pypi_info(license_expression="0BSD"), "0BSD"),
        (pypi_info(license_expression="MIT OR Apache-2.0"), "MIT OR Apache-2.0"),
        # the SPDX expression wins over the less precise legacy field and classifiers
        (
            pypi_info(
                license_expression="AGPL-3.0-only",
                classifiers=["License :: OSI Approved :: GNU Affero General Public License v3"],
            ),
            "AGPL-3.0-only",
        ),
        # packages without an SPDX expression fall back to those
        (pypi_info(license="Apache-2.0"), "Apache-2.0"),
        (pypi_info(classifiers=["License :: OSI Approved :: MIT License"]), "MIT License"),
        (
            pypi_info(
                license="Apache-2.0",
                classifiers=["License :: OSI Approved :: Apache Software License"],
            ),
            "Apache-2.0",
        ),
        (
            pypi_info(classifiers=["Programming Language :: Python", "License :: OSI Approved"]),
            "Unknown",
        ),
        # a classifier that is no more than the "License" segment names one no more than that does
        (pypi_info(classifiers=["License"]), "Unknown"),
        # several classifiers: the one that fails the check decides, whatever its position
        (
            pypi_info(
                classifiers=[
                    "License :: OSI Approved :: MIT License",
                    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
                ]
            ),
            "GNU General Public License v3 (GPLv3)",
        ),
        (
            pypi_info(
                classifiers=[
                    "License :: OSI Approved :: Apache Software License",
                    "License :: OSI Approved :: MIT License",
                ]
            ),
            "Apache Software License",
        ),
        # two-part classifiers name a license too, and must not be hidden by a permissive one
        (
            pypi_info(
                classifiers=[
                    "License :: Other/Proprietary License",
                    "License :: OSI Approved :: MIT License",
                ]
            ),
            "Other/Proprietary License",
        ),
        (pypi_info(classifiers=["License :: Public Domain"]), "Public Domain"),
        # nothing at all to go on
        (pypi_info(), "Unknown"),
        (pypi_info(license="   "), "Unknown"),
    ],
)
def test_get_package_license(info: dict[str, Any], expected: str) -> None:
    """Test the license is resolved from any of the fields PyPI exposes it in."""
    assert get_package_license(info)[0] == expected


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (pypi_info(license_expression="0BSD"), True),
        (pypi_info(license="0BSD"), False),
        (pypi_info(classifiers=["License :: OSI Approved :: MIT License"]), False),
        (pypi_info(), False),
    ],
)
def test_get_package_license_reports_spdx(info: dict[str, Any], expected: bool) -> None:
    """Test only a PEP 639 expression is reported as one."""
    assert get_package_license(info)[1] is expected


@pytest.mark.parametrize(
    ("license_str", "expected"),
    [
        # an expression is validated by PyPI, so what the evaluator rejects is simply not allowed,
        # rather than wording we failed to read
        ("MIT AND Frobnicate-1.0", False),
        # a malformed expression names nothing we can check, so it is not compatible either
        ("MIT OR AND", False),
        ("MIT WITH OR", False),
        # "or later" is a single marker, not a way to dress up an unknown identifier
        ("MIT++++", False),
        ("LGPL-2.1+", True),
        ("MIT OR (Apache-2.0", False),
        ("BSD-3-Clause-No-Nuclear-License-2014", False),
        ("LicenseRef-Proprietary", False),
        # an expression is never license prose, so a custom identifier cannot smuggle in the
        # wording of a grant to be read as the license it belongs to
        (
            "LicenseRef-Permission-is-hereby-granted-free-of-charge-to-any-person-obtaining-a"
            "-copy-of-this-software-and-associated-documentation-files",
            False,
        ),
        ("0BSD", True),
        ("MIT OR Apache-2.0", True),
        # a group has to be closed for what follows it to be read as part of the expression
        ("(MIT) OR (Apache-2.0)", True),
        # an alternative we do not know does not spoil one we do
        ("Frobnicate-1.0 OR MIT", True),
        ("Apache-2.0 WITH LLVM-exception", True),
    ],
)
def test_spdx_expressions_are_not_guessed_at(license_str: str, expected: bool) -> None:
    """Test an SPDX expression is judged on its identifiers only."""
    assert check_license_compatibility(license_str, True)[0] is expected


@pytest.mark.parametrize(
    "license_str",
    [
        # SPDX identifiers as used in a PEP 639 expression
        "0BSD",
        "MIT",
        "MIT-0",
        "Apache-2.0",
        "BSD-3-Clause",
        "MPL-2.0",
        "LGPL-2.1-or-later",
        "MIT OR Apache-2.0",
        "Apache-2.0 OR BSD-3-Clause",
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "MPL-2.0 AND (Apache-2.0 OR MIT)",
        "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND MIT",
        # legacy license strings and classifier names keep working
        "BSD",
        "MIT License",
        "Apache Software License",
        "GNU Lesser General Public License v3 (LGPLv3)",
        "ISC License (ISCL)",
        "PSFL",
        "LGPLv2+",
        "Public Domain",
        # a plain license field can hold an expression too (aiohttp publishes this one)
        "Apache-2.0 AND MIT",
        "The MIT License (MIT)",
        "CC0 1.0 Universal",
        # spelling variants of the same licenses
        "MPL 2.0",
        "Apache 2.0 License",
        "MIT license",
        "The MIT License",
        "MIT Licence",
        # a name holding a comma is matched whole, before the value is read as a list
        "Apache License, Version 2.0",
        # spellings the BSD family is published under (protobuf, jsonpatch)
        "3-Clause BSD License",
        "Modified BSD License",
        # every license of a value that lists several (pycryptodome publishes this one)
        "BSD, Public Domain",
        # ...however the value joins them (uritemplate publishes the first)
        "BSD 3-Clause OR Apache-2.0",
        "MIT/Apache-2.0",
        "MIT License AND Apache Software License",
        "MIT and/or Apache-2.0",
        # a name holding a separator is still matched whole, before the value is split on one
        "zlib/libpng License",
        "GNU Library or Lesser General Public License (LGPL)",
        "Historical Permission Notice and Disclaimer (HPND)",
        # a custom license alongside one we accept still leaves a usable option
        "MIT OR LicenseRef-Proprietary",
        # an exception only widens what the license allows, so the license itself decides
        "Zlib WITH LLVM-exception",
        "LGPL-3.0-only WITH LGPL-3.0-linking-exception",
        # packages that put their whole license text in the field are read on the grant it
        # spells out, whatever heading and punctuation surround it (ya-dialogs-api, aiomusiccast)
        "MIT License\n\n        Copyright (c) 2026 Mikhail Nevskiy\n\n        Permission is"
        " hereby granted, free of charge, to any person obtaining a copy\n        of this"
        ' software and associated documentation files (the "Software"), to deal\n        in the'
        " Software without restriction.",
        "**The MIT License (MIT)**  Copyright &copy; 2021, Tom Schneider  Permission is hereby"
        " granted, free of charge, to any person obtaining a copy of this software and"
        ' associated documentation files (the "Software"), to deal in the Software without'
        " restriction.",
        "Copyright (c) 2026\n\nPermission to use, copy, modify, and/or distribute this software"
        " for any purpose with or without fee is hereby granted.",
        "Redistribution and use in source and binary forms, with or without modification, are"
        " permitted provided that the following conditions are met.",
        'Licensed under the Apache License, Version 2.0 (the "License"); you may not use this'
        " file except in compliance with the License.",
    ],
)
def test_compatible_licenses(license_str: str) -> None:
    """Test permissive licenses are accepted."""
    compatible, status = check_license_compatibility(license_str)
    assert compatible, status


def test_license_text_is_read_on_its_grant_only() -> None:
    """Test a license text is accepted on the grant it spells out, terms added to it aside."""
    # a grant identifies the license it belongs to, but says nothing about clauses written after
    # it, so a text adding one is still accepted. Recognising those would mean comparing against
    # the complete text of every license, which this check does not attempt
    restricted = (
        "Permission is hereby granted, free of charge, to any person obtaining a copy of this"
        ' software and associated documentation files (the "Software"), to deal in the Software'
        " without restriction.\n\nThe Software shall be used for Good, not Evil."
    )

    assert check_license_compatibility(restricted)[0]


@pytest.mark.parametrize(
    ("license_str", "expected_status"),
    [
        ("GPL-3.0-only", "Incompatible copyleft license (GPL-3.0-only)"),
        ("AGPL-3.0-only", "Incompatible copyleft license (AGPL-3.0-only)"),
        # a permissive term must not mask a copyleft one it is combined with, whether or not the
        # expression around it parses
        ("MIT AND GPL-3.0-only", "Incompatible copyleft license (MIT AND GPL-3.0-only)"),
        ("(GPL-3.0-only AND MIT", "Incompatible copyleft license"),
        ("MIT OR (GPL-3.0-only", "Incompatible copyleft license"),
        ("LicenseRef-MIT Custom", "Unknown/unverified license"),
        # only understood in part is not understood: "Zlib" alone would be compatible
        ("Zlib plus custom terms", "Unknown/unverified license"),
        ("GNU General Public License v3 (GPLv3)", "Incompatible copyleft license"),
        # an LGPL term in the string does not excuse a GPL one standing next to it
        ("LGPL plus GPL terms", "Incompatible copyleft license"),
        # an exception widens a license, so a copyleft one cannot be hiding behind "WITH"
        ("MIT WITH GPL-3.0-only", "Incompatible copyleft license"),
        ("Apache-2.0 AND MIT WITH GPL-3.0-only", "Incompatible copyleft license"),
        ("LGPL-2.1-or-later AND GPL-3.0-only", "Incompatible copyleft license"),
        ("Frobnicate-1.0", "Unknown/unverified license (Frobnicate-1.0)"),
        # a license name has to be named, not spelled out by unrelated words running together
        ("This copyright notice shall be included in all copies", "Unknown/unverified license"),
        ("Redistribution is permitted for internal use only", "Unknown/unverified license"),
        ("SUBMITTED-1.0", "Unknown/unverified license"),
        ("Mitigation License 1.0", "Unknown/unverified license"),
        # a name that merely starts like one we know is not that license
        ("MITX", "Unknown/unverified license"),
        # prose that says the opposite of the license it names
        ("This software is not in the public domain. All rights reserved.", "Unknown/unverified"),
        ("ISC2", "Unknown/unverified license"),
        ("Internal use only, do not transmit", "Unknown/unverified license"),
        # a name we accept does not carry the terms written around it, however it is joined to
        # them, and an SPDX operator between prose words is not an expression to read it out of
        ("MIT License AND Proprietary", "Unknown/unverified license"),
        ("MIT License for non-commercial use only", "Unknown/unverified license"),
        ("MIT plus commercial terms", "Unknown/unverified license"),
        ("BSD, Proprietary", "Unknown/unverified license"),
        # a license text is only recognised by the grant it spells out, not by its heading
        ("MIT License\n\nAll rights reserved. Contact us for terms.", "Unknown/unverified"),
        # ...and the grant has to be the one the text opens, not the tail of another word
        (
            "Nonpermission is hereby granted, free of charge, to any person obtaining a copy of"
            " this software and associated documentation files.",
            "Unknown/unverified license",
        ),
        # a text that breaks off the grant to restrict it does not spell out that grant
        (
            "Permission is hereby granted, free of charge, to any person obtaining a copy solely"
            " for non-commercial use.",
            "Unknown/unverified license",
        ),
        # ...and neither does one that denies it outright, however the denial is worded
        (
            "No permission is hereby granted, free of charge, to any person obtaining a copy of"
            " this software and associated documentation files.",
            "Unknown/unverified license",
        ),
        (
            "No additional permission is hereby granted, free of charge, to any person obtaining"
            " a copy of this software and associated documentation files.",
            "Unknown/unverified license",
        ),
        # a grant is quoted to its last word, so a text trailing off into another license is not
        # taken for the one it started as
        (
            'Licensed under the Apache License, Version 2.0 (the "License"); you may not use this'
            " file except in compliance with the Proprietary License",
            "Unknown/unverified license",
        ),
        # a copyleft license the text is combined with is not excused by the grant it spells out
        (
            "MIT License AND GPL-3.0-only\n\nPermission is hereby granted, free of charge, to any"
            " person obtaining a copy of this software and associated documentation files.",
            "Incompatible copyleft license",
        ),
        # neither alternative of an expression is one we know, so the expression is not either
        ("Frobnicate-1.0 OR Frobnicate-2.0", "Unknown/unverified license"),
        # a value that is only separators names nothing
        (",", "Unknown/unverified license"),
        # a license we accept does not carry the one it is offered alongside
        ("MIT or Proprietary Terms", "Unknown/unverified license"),
        # a term we cannot read is still a term, in whatever script it is written
        ("MIT 非商用", "Unknown/unverified license"),
        ("Apache 2.0 нельзя", "Unknown/unverified license"),
        # a custom license names itself, whatever wording its identifier is built out of
        (
            "LicenseRef-Permission-is-hereby-granted-free-of-charge-to-any-person-obtaining-a"
            "-copy-of-this-software-and-associated-documentation-files",
            "Unknown/unverified license",
        ),
        # groups in prose are not an expression, and no longer a name to read out of it either
        ("MIT License (a) (b) (c) (d) (e) (f) (g) and so on", "Unknown/unverified license"),
        # a value that joins licenses is read as an expression, whichever field it came from
        ("MIT AND Proprietary", "Unknown/unverified license"),
        ("MIT AND(Proprietary)", "Unknown/unverified license"),
        ("MIT AND (Proprietary", "Unknown/unverified license"),
        ("Other/Proprietary License", "Unknown/unverified license"),
        # a custom license is never pre-approved, not even when its name reads permissive
        (
            "LicenseRef-Proprietary-MIT-Terms",
            "Unknown/unverified license (LicenseRef-Proprietary-MIT-Terms)",
        ),
        ("Unknown", "No license information"),
        ("", "No license information"),
        # nesting deep enough to exhaust the stack is refused, not approved on the name inside
        ("(" * 333 + "MIT" + ")" * 333, "Unknown/unverified license"),
    ],
)
def test_incompatible_licenses(license_str: str, expected_status: str) -> None:
    """Test copyleft and unrecognised licenses are rejected."""
    compatible, status = check_license_compatibility(license_str)
    assert not compatible
    assert status.startswith(expected_status)


def test_check_package_reads_license_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test a package that only declares an SPDX expression passes the license check."""
    metadata = {
        "info": {
            "version": "7.6.0",
            "license": None,
            "license_expression": "0BSD",
            "classifiers": [],
            "author": "Dan Blanchard",
            "summary": "Universal encoding detector",
            "project_urls": {"Homepage": "https://github.com/chardet/chardet"},
        },
        "releases": {
            f"{major}.0.0": [{"upload_time": "2015-01-01T00:00:00"}] for major in range(1, 5)
        },
    }
    monkeypatch.setattr(check_package_safety, "get_pypi_metadata", lambda _: metadata)

    result = check_package("chardet")

    assert result["license"] == "0BSD"
    assert result["automated_checks"]["license_compatible"]
    assert result["check_details"]["license"] == "Compatible (0BSD)"
    assert not [warning for warning in result["warnings"] if "License" in warning]
