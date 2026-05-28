"""
Blackboard CLI


    python -m team_layer.blackboard list                   #
    python -m team_layer.blackboard list --scope shared
    python -m team_layer.blackboard list --status doing
    python -m team_layer.blackboard view --scope shared    # Kanban
    python -m team_layer.blackboard post 'fix login bug' --author alice
    python -m team_layer.blackboard update <id> --status done --author alice
    python -m team_layer.blackboard history <id>
"""

from .cli import main

if __name__ == "__main__":
    main()
