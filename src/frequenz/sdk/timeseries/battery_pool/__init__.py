# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""Manage a pool of batteries."""

from ._battery_pool_wrapper import BatteryPoolWrapper
from ._result_types import PowerMetrics

__all__ = [
    "BatteryPoolWrapper",
    "PowerMetrics",
]
