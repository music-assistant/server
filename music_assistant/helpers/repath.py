"""
Helper functionalities for path detection in api routes.

Based on the original work by nickcoutsos and synacor
https://github.com/nickcoutsos/python-repath
"""
import re
import urllib
import urllib.parse

REGEXP_TYPE = type(re.compile(""))
PATH_REGEXP = re.compile(
    "|".join(
        [
            # Match escaped characters that would otherwise appear in future matches.
            # This allows the user to escape special characters that won't transform.
            "(\\\\.)",
            # Match Express-style parameters and un-named parameters with a prefix
            # and optional suffixes. Matches appear as:
            #
            # "/:test(\\d+)?" => ["/", "test", "\d+", undefined, "?", undefined]
            # "/route(\\d+)"  => [undefined, undefined, undefined, "\d+", undefined, undefined]
            # "/*"            => ["/", undefined, undefined, undefined, undefined, "*"]
            "([\\/.])?(?:(?:\\:(\\w+)(?:\\(((?:\\\\.|[^()])+)\\))?|\\(((?:\\\\.|[^()])+)\\))([+*?])?|(\\*))",
        ]
    )
)


def escape_string(string):
    """Escape URL-acceptable regex special-characters."""
    return re.sub("([.+*?=^!:${}()[\\]|])", r"\\\1", string)


def escape_group(group):
    """Escape group."""
    return re.sub("([=!:$()])", r"\\\1", group)


def parse(string):
    """Parse a string for the raw tokens."""
    tokens = []
    key = 0
    index = 0
    path = ""

    for match in PATH_REGEXP.finditer(string):
        matched = match.group(0)
        escaped = match.group(1)
        offset = match.start(0)
        path += string[index:offset]
        index = offset + len(matched)

        if escaped:
            path += escaped[1]
            continue

        if path:
            tokens.append(path)
            path = ""

        prefix, name, capture, group, suffix, asterisk = match.groups()[1:]
        repeat = suffix in ("+", "*")
        optional = suffix in ("?", "*")
        delimiter = prefix or "/"
        pattern = capture or group or (".*" if asterisk else "[^%s]+?" % delimiter)

        if not name:
            name = key
            key += 1

        token = {
            "name": str(name),
            "prefix": prefix or "",
            "delimiter": delimiter,
            "optional": optional,
            "repeat": repeat,
            "pattern": escape_group(pattern),
        }

        tokens.append(token)

    if index < len(string):
        path += string[index:]

    if path:
        tokens.append(path)

    return tokens


def tokens_to_function(tokens):
    """Expose a method for transforming tokens into the path function."""

    def transform(obj):
        path = ""
        obj = obj or {}

        for key in tokens:
            if isinstance(key, str):
                path += key
                continue

            regexp = re.compile("^%s$" % key["pattern"])

            value = obj.get(key["name"])
            if value is None:
                if key["optional"]:
                    continue
                raise KeyError('Expected "{name}" to be defined'.format(**key))

            if isinstance(value, list):
                if not key["repeat"]:
                    raise TypeError('Expected "{name}" to not repeat'.format(**key))

                if not value:
                    if key["optional"]:
                        continue
                    raise ValueError('Expected "{name}" to not be empty'.format(**key))

                for i, val in enumerate(value):
                    val = str(val)
                    if not regexp.search(val):
                        raise ValueError(
                            'Expected all "{name}" to match "{pattern}"'.format(**key)
                        )

                    path += key["prefix"] if i == 0 else key["delimiter"]
                    path += urllib.parse.quote(val, "")

                continue

            value = str(value)
            if not regexp.search(value):
                raise ValueError('Expected "{name}" to match "{pattern}"'.format(**key))

            path += key["prefix"] + urllib.parse.quote(
                value.encode("utf8"), "-_.!~*'()"
            )

        return path

    return transform


def regexp_to_pattern(regexp, keys):
    """
    Generate a pattern based on a compiled regular expression.

    This function exists for a semblance of compatibility with pathToRegexp
    and serves basically no purpose beyond making sure the pre-existing tests
    continue to pass.

    """
    _match = re.search(r"\((?!\?)", regexp.pattern)

    if _match:
        keys.extend(
            [
                {
                    "name": i,
                    "prefix": None,
                    "delimiter": None,
                    "optional": False,
                    "repeat": False,
                    "pattern": None,
                }
                for i in range(len(_match.groups()))
            ]
        )

    return regexp.pattern


def tokens_to_pattern(tokens, options=None):
    """Generate a pattern for the given list of tokens."""
    options = options or {}

    strict = options.get("strict")
    end = options.get("end") is not False
    route = ""
    last_token = tokens[-1]
    ends_with_slash = isinstance(last_token, str) and last_token.endswith("/")

    patterns = dict(
        REPEAT="(?:{prefix}{capture})*",
        OPTIONAL="(?:{prefix}({name}{capture}))?",
        REQUIRED="{prefix}({name}{capture})",
    )

    for token in tokens:
        if isinstance(token, str):
            route += escape_string(token)
            continue

        parts = {
            "prefix": escape_string(token["prefix"]),
            "capture": token["pattern"],
            "name": "",
        }

        if token["name"] and re.search("[a-zA-Z]", token["name"]):
            parts["name"] = "?P<%s>" % re.escape(token["name"])

        if token["repeat"]:
            parts["capture"] += patterns["REPEAT"].format(**parts)

        template = patterns["OPTIONAL" if token["optional"] else "REQUIRED"]
        route += template.format(**parts)

    if not strict:
        route = route[:-1] if ends_with_slash else route
        route += "(?:/(?=$))?"

    if end:
        route += "$"
    else:
        route += "" if strict and ends_with_slash else "(?=/|$)"

    return "^%s" % route


def array_to_pattern(paths, keys, options):
    """Generate a single pattern from an array of path pattern values."""
    parts = [path_to_pattern(path, keys, options) for path in paths]

    return "(?:%s)" % ("|".join(parts))


def string_to_pattern(path, keys, options):
    """
    Generate pattern for a string.

    Equivalent to `tokens_to_pattern(parse(string))`.
    """
    tokens = parse(path)
    pattern = tokens_to_pattern(tokens, options)

    tokens = filter(lambda t: not isinstance(t, str), tokens)
    keys.extend(tokens)

    return pattern


def path_to_pattern(path, keys=None, options=None):
    """
    Generate a pattern from any kind of path value.

    This function selects the appropriate function array/regex/string paths,
    and calls it with the provided values.
    """
    keys = keys if keys is not None else []
    options = options if options is not None else {}

    if isinstance(path, REGEXP_TYPE):
        return regexp_to_pattern(path, keys)
    if isinstance(path, list):
        return array_to_pattern(path, keys, options)
    return string_to_pattern(path, keys, options)


def match_pattern(pattrn, requested_url_path):
    """Return shorthand to match function."""
    return re.match(pattrn, requested_url_path)
