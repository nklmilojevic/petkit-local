"""The version must be the same number in all three places that state it.

`config.yaml` is what the Supervisor offers as an update, `pyproject.toml` is
what the package says it is, and `utils/const.VERSION` is what the panel shows
the user. They are three separate files, and a release that bumps two of them
leaves the running add-on reporting a version nobody shipped — which is worse
than showing nothing, because it is believed.
"""
import re
from pathlib import Path

from petkit_local.utils.const import VERSION

ADDON = Path(__file__).resolve().parent.parent


def _config_yaml_version() -> str:
    # Read as text rather than with a YAML parser: this must hold even if PyYAML
    # is not installed in the environment running the tests, and the field is a
    # plain quoted scalar on its own line.
    text = (ADDON / "config.yaml").read_text()
    match = re.search(r'^version:\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "config.yaml has no `version:` line"
    return match.group(1)


def _pyproject_version() -> str:
    text = (ADDON / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no `version =` line"
    return match.group(1)


def test_the_three_declared_versions_agree():
    assert VERSION == _config_yaml_version() == _pyproject_version(), (
        f"const.VERSION={VERSION!r}, config.yaml={_config_yaml_version()!r}, "
        f"pyproject.toml={_pyproject_version()!r} — bump all three"
    )


def test_the_version_is_a_plain_release_number():
    """The Supervisor compares add-on versions to decide whether an update is
    available, so a stray suffix means the update is never offered."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION


def test_the_changelog_documents_the_current_version():
    """A release whose changelog was forgotten is one nobody can tell apart from
    the one before it."""
    changelog = (ADDON / "CHANGELOG.md").read_text()
    assert re.search(rf"^## v?{re.escape(VERSION)}\b", changelog, re.MULTILINE), (
        f"CHANGELOG.md has no `## {VERSION}` section"
    )
