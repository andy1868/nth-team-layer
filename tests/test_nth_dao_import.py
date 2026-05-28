import importlib
import pathlib
import tomllib

import nth_dao


def test_nth_dao_is_the_only_public_package_name():
    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert data["project"]["name"] == "nth-dao"
    assert "nth_dao" in data["tool"]["setuptools"]["packages"]
    assert "nth_team_layer" not in data["tool"]["setuptools"]["packages"]


def test_nth_dao_import_path_exports_current_api():
    assert nth_dao.attach
    assert nth_dao.GroupManager
    assert nth_dao.TeamRole


def test_nth_dao_submodule_imports_work():
    membership = importlib.import_module("nth_dao.membership")
    orchestration = importlib.import_module("nth_dao.orchestration")

    assert membership.TeamRole.OWNER == nth_dao.TeamRole.OWNER
    assert orchestration.Mission
