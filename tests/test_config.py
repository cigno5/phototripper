import phototripper.sort  # noqa: F401  -- importing a module registers its settings
from phototripper.common import (
    COMMON_DEFAULTS,
    apply_config_to_args,
    load_app_config,
    register_defaults,
    registered_modules,
)


class Args:
    """Stand-in for the argparse namespace."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_module_settings_are_scoped():
    assert "rename-format" in load_app_config("sort")
    assert "rename-format" not in load_app_config("gpx")
    assert "rename-format" not in load_app_config(None)

    # every module always sees the shared settings
    for key in COMMON_DEFAULTS:
        assert key in load_app_config("gpx")


def test_sort_registers_itself():
    assert "sort" in registered_modules()


def test_layering(ini):
    ini("""
[common]
search-radius = 100
verbose = true

[sort]
search-radius = 200
""")

    # a module owned key can still be set from [common]...
    assert load_app_config("sort")["search-radius"] == 200
    assert load_app_config("sort")["verbose"] is True

    # ...and the shared value applies when the module says nothing
    ini("""
[common]
search-radius = 100
""")
    assert load_app_config("sort")["search-radius"] == 100


def test_unknown_keys_and_foreign_sections_are_ignored(ini):
    ini("""
[common]
not-a-setting = 1

[gpx]
rename-format = {day}
""")
    config = load_app_config("gpx")
    assert "not-a-setting" not in config
    assert "rename-format" not in config


def test_type_conversion(ini):
    ini("""
[sort]
search-radius = 4200
debug = yes
rename-only = off
""")
    config = load_app_config("sort")
    assert config["search-radius"] == 4200
    assert config["debug"] is True
    assert config["rename-only"] is False


def test_cli_wins_over_config(ini):
    ini("""
[sort]
search-radius = 200
""")
    args = Args(search_radius=999, cache=None)
    apply_config_to_args(args, "sort")
    assert args.search_radius == 999  # explicit CLI value untouched
    assert args.cache is None  # backfilled from the hardcoded default


def test_config_fills_unset_args(ini):
    ini("""
[sort]
search-radius = 200
""")
    args = Args(search_radius=None)
    apply_config_to_args(args, "sort")
    assert args.search_radius == 200


def test_foreign_attributes_are_never_touched(ini):
    ini("[sort]\nsearch-radius = 200\n")
    args = Args(search_radius=None, something_else=None)
    apply_config_to_args(args, "sort")
    assert args.something_else is None


def test_register_defaults_is_idempotent():
    register_defaults("throwaway", {"a-key": 1}, ints={"a-key"})
    register_defaults("throwaway", {"a-key": 2}, ints={"a-key"})
    assert load_app_config("throwaway")["a-key"] == 2
