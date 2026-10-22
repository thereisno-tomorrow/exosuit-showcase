"""Pure configuration example. The private live release runner is excluded."""


def compact_settings(existing):
    """Retain unrelated settings while selecting the compact tool surface."""
    value = dict(existing)
    value.update(tool_visibility="brain-and-more", native_tool_docs="on-demand",
                 always_ready_tools=["bc_memory_save", "bc_memory_list", "bc_memory_change",
                                     "bc_memory_search", "bc_work", "gremlin_town_review_respond"])
    return value


if __name__ == "__main__":
    raise SystemExit("Example configuration helper only; no live switch is bundled.")
