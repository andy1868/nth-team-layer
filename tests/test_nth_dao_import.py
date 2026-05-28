import nth_dao
import nth_team_layer


def test_nth_dao_import_path_reexports_current_api():
    assert nth_dao.attach is nth_team_layer.attach
    assert nth_dao.GroupManager is nth_team_layer.GroupManager
    assert nth_dao.TeamRole is nth_team_layer.TeamRole


def test_nth_dao_submodule_aliases_work():
    from nth_dao.membership import TeamRole

    assert TeamRole.OWNER == nth_team_layer.TeamRole.OWNER
