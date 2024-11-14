# License: MIT
# Copyright © 2024 Frequenz Energy-as-a-Service GmbH

"""Utilities to deal with configuration."""

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from marshmallow_dataclass import class_schema

T = TypeVar("T")
"""Type variable for configuration classes."""


def load_config(
    cls: type[T],
    config: Mapping[str, Any],
    /,
    **marshmallow_load_kwargs: Any,
) -> T:
    """Load a configuration from a dictionary into an instance of a configuration class.

    The configuration class is expected to be a [`dataclasses.dataclass`][], which is
    used to create a [`marshmallow.Schema`][] schema to validate the configuration
    dictionary using [`marshmallow_dataclass.class_schema`][] (which in turn uses the
    [`marshmallow.Schema.load`][] method to do the validation and deserialization).

    To customize the schema derived from the configuration dataclass, you can use the
    `metadata` key in [`dataclasses.field`][] to pass extra options to
    [`marshmallow_dataclass`][] to be used during validation and deserialization.

    Additional arguments can be passed to [`marshmallow.Schema.load`][] using keyword
    arguments `marshmallow_load_kwargs`.

    Note:
        This method will raise [`marshmallow.ValidationError`][] if the configuration
        dictionary is invalid and you have to have in mind all of the gotchas of
        [`marshmallow`][] and [`marshmallow_dataclass`][] applies when using this
        function.  It is recommended to carefully read the documentation of these
        libraries.

    Args:
        cls: The configuration class.
        config: The configuration dictionary.
        **marshmallow_load_kwargs: Additional arguments to be passed to
            [`marshmallow.Schema.load`][].

    Returns:
        The loaded configuration as an instance of the configuration class.
    """
    instance = class_schema(cls)().load(config, **marshmallow_load_kwargs)
    # We need to cast because `.load()` comes from marshmallow and doesn't know which
    # type is returned.
    return cast(T, instance)
