from dataclasses import replace

import pytest
from writai import config


def test_production_rejects_documented_generator_comment_as_secret() -> None:
    publicly_known_comment = (
        '# required outside development: python3 -c '
        '"import secrets;print(secrets.token_urlsafe(48))"'
    )
    production = replace(
        config.Settings(),
        env="production",
        grant_secret=publicly_known_comment,
    )

    with pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET"):
        config.require_production_secrets(production)
