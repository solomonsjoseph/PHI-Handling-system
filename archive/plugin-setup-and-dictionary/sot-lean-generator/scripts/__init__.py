"""Skill script package marker (Note 19 plugin consolidation).

The pipeline modules in this directory physically live here but are imported
under their original ``scripts.*`` canonical names via the migration bridge in
``scripts/__init__.py`` (a ``sys.meta_path`` finder). The skill's CLI entry
points are also runnable directly by file path. The hyphenated parent skill
directory is not an importable Python package, so this marker exists for tooling
and future subprocess entry points, not for ``import``-time package resolution.
"""
