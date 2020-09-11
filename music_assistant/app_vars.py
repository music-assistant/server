"""Some magic to store some appvars."""
# pylint: skip-file
# flake8: noqa
(
    lambda __g: [
        [
            [
                [
                    None
                    for __g["get_app_var"], get_app_var.__name__ in [
                        (
                            lambda index: (
                                lambda __l: [
                                    APP_VARS[__l["index"]] for __l["index"] in [(index)]
                                ][0]
                            )({}),
                            "get_app_var",
                        )
                    ]
                ][0]
                for __g["APP_VARS"] in [
                    (base64.b64decode(VARS_ENC).decode("utf-8").split(","))
                ]
            ][0]
            for __g["VARS_ENC"] in [
                (
                    b"OTQyODUyNTY3LDc2MTczMGQzZjk1ZTRhZjA5YWM2M2I5YTM3Y2NjOTZhLDJlYjk2ZjliMzc0OTRiZTE4MjQ5OTlkNTgwMjhhMzA1LFNTcnRNMnhlM2wwMDNnOEh4RmVUUUtub3BaNklCaUwzRTlPc1QxODFYMDA9"
                )
            ]
        ][0]
        for __g["base64"] in [(__import__("base64", __g, __g))]
    ][0]
)(globals())
