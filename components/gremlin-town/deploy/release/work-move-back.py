"""Hashed work move-back entry point; shared code is in the installed wheel."""
import sys

# Match Python -P for standalone path/stdin execution, without removing an
# explicit PYTHONPATH entry when the interpreter already uses safe_path.
if __name__ == "__main__" and not sys.flags.safe_path:
    del sys.path[:1]

from gremlin_town.work_model_migration import (
    FIXTURE_MARKER,
    MovePlan,
    plan_move_over,
    plan_move_back,
    load_hub_settings,
    build_hub_storage,
    pre_move_inventory,
    register_review_bootstrap,
    load_rules,
    record_path,
    format_plan,
    materialize,
    validate_credentials,
    apply_local,
    observe_migration,
    apply_storage,
    main as migration_main,
)


def main(arguments=None, **kwargs) -> int:
    """Run the installed migration CLI in back mode, dry by default."""
    return migration_main(arguments, direction="back", **kwargs)


if __name__ == "__main__":
    sys.exit(main())
