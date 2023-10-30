# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""Tests for the Matryoshka power manager algorithm."""

from datetime import datetime, timezone

from frequenz.sdk import timeseries
from frequenz.sdk.actor._power_managing import Proposal
from frequenz.sdk.actor._power_managing._matryoshka import Matryoshka
from frequenz.sdk.timeseries import Power, battery_pool


class StatefulTester:
    """A stateful tester for the Matryoshka algorithm."""

    def __init__(
        self,
        batteries: frozenset[int],
        system_bounds: battery_pool.PowerMetrics,
    ) -> None:
        """Create a new instance of the stateful tester."""
        self._call_count = 0
        self._batteries = batteries
        self._algorithm = Matryoshka()
        self._system_bounds = system_bounds

    def tgt_power(  # pylint: disable=too-many-arguments
        self,
        priority: int,
        power: float | None,
        bounds: tuple[float | None, float | None],
        expected: float | None,
        must_send: bool = False,
    ) -> None:
        """Test the target power calculation."""
        self._call_count += 1
        tgt_power = self._algorithm.calculate_target_power(
            self._batteries,
            Proposal(
                battery_ids=self._batteries,
                source_id=f"actor-{priority}",
                preferred_power=None if power is None else Power.from_watts(power),
                bounds=timeseries.Bounds(
                    None if bounds[0] is None else Power.from_watts(bounds[0]),
                    None if bounds[1] is None else Power.from_watts(bounds[1]),
                ),
                priority=priority,
            ),
            self._system_bounds,
            must_send,
        )
        assert tgt_power == (
            Power.from_watts(expected) if expected is not None else None
        )

    def bounds(
        self,
        priority: int,
        expected_power: float | None,
        expected_bounds: tuple[float, float],
    ) -> None:
        """Test the status report."""
        report = self._algorithm.get_status(
            self._batteries, priority, self._system_bounds, None
        )
        if expected_power is None:
            assert report.target_power is None
        else:
            assert report.target_power is not None
            assert report.target_power.as_watts() == expected_power
        assert report.inclusion_bounds is not None
        assert report.inclusion_bounds.lower.as_watts() == expected_bounds[0]
        assert report.inclusion_bounds.upper.as_watts() == expected_bounds[1]


async def test_matryoshka_algorithm() -> None:  # pylint: disable=too-many-statements
    """Tests for the power managing actor."""
    batteries = frozenset({2, 5})

    algorithm = Matryoshka()
    system_bounds = battery_pool.PowerMetrics(
        timestamp=datetime.now(tz=timezone.utc),
        inclusion_bounds=timeseries.Bounds(
            lower=Power.from_watts(-200.0), upper=Power.from_watts(200.0)
        ),
        exclusion_bounds=timeseries.Bounds(lower=Power.zero(), upper=Power.zero()),
    )

    call_count = 0

    def test_tgt_power(
        priority: int,
        power: float | None,
        bounds: tuple[float | None, float | None],
        expected: float | None,
        must_send: bool = False,
    ) -> None:
        nonlocal call_count
        call_count += 1
        tgt_power = algorithm.calculate_target_power(
            batteries,
            Proposal(
                battery_ids=batteries,
                source_id=f"actor-{priority}",
                preferred_power=None if power is None else Power.from_watts(power),
                bounds=timeseries.Bounds(
                    None if bounds[0] is None else Power.from_watts(bounds[0]),
                    None if bounds[1] is None else Power.from_watts(bounds[1]),
                ),
                priority=priority,
            ),
            system_bounds,
            must_send,
        )
        assert tgt_power == (
            Power.from_watts(expected) if expected is not None else None
        )

    def test_bounds(
        priority: int,
        expected_power: float | None,
        expected_bounds: tuple[float, float],
    ) -> None:
        report = algorithm.get_status(batteries, priority, system_bounds, None)
        if expected_power is None:
            assert report.target_power is None
        else:
            assert report.target_power is not None
            assert report.target_power.as_watts() == expected_power
        assert report.inclusion_bounds is not None
        assert report.inclusion_bounds.lower.as_watts() == expected_bounds[0]
        assert report.inclusion_bounds.upper.as_watts() == expected_bounds[1]

    test_tgt_power(priority=2, power=25.0, bounds=(25.0, 50.0), expected=25.0)
    test_bounds(priority=2, expected_power=25.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=1, expected_power=25.0, expected_bounds=(25.0, 50.0))

    test_tgt_power(priority=1, power=20.0, bounds=(20.0, 50.0), expected=None)
    test_tgt_power(
        priority=1, power=20.0, bounds=(20.0, 50.0), expected=25.0, must_send=True
    )
    test_bounds(priority=1, expected_power=25.0, expected_bounds=(25.0, 50.0))

    test_tgt_power(priority=3, power=10.0, bounds=(10.0, 15.0), expected=15.0)
    test_bounds(priority=3, expected_power=15.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=15.0, expected_bounds=(10.0, 15.0))
    test_bounds(priority=1, expected_power=15.0, expected_bounds=(10.0, 15.0))

    test_tgt_power(priority=3, power=10.0, bounds=(10.0, 22.0), expected=22.0)
    test_bounds(priority=3, expected_power=22.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=22.0, expected_bounds=(10.0, 22.0))
    test_bounds(priority=1, expected_power=22.0, expected_bounds=(10.0, 22.0))

    test_tgt_power(priority=1, power=30.0, bounds=(20.0, 50.0), expected=None)
    test_bounds(priority=1, expected_power=22.0, expected_bounds=(10.0, 22.0))

    test_tgt_power(priority=3, power=10.0, bounds=(10.0, 50.0), expected=30.0)
    test_bounds(priority=3, expected_power=30.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=30.0, expected_bounds=(10.0, 50.0))
    test_bounds(priority=1, expected_power=30.0, expected_bounds=(25.0, 50.0))

    test_tgt_power(priority=2, power=40.0, bounds=(40.0, None), expected=40.0)
    test_bounds(priority=3, expected_power=40.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=40.0, expected_bounds=(10.0, 50.0))
    test_bounds(priority=1, expected_power=40.0, expected_bounds=(40.0, 50.0))

    test_tgt_power(priority=2, power=0.0, bounds=(None, None), expected=30.0)
    test_bounds(priority=4, expected_power=30.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=3, expected_power=30.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=30.0, expected_bounds=(10.0, 50.0))
    test_bounds(priority=1, expected_power=30.0, expected_bounds=(10.0, 50.0))

    test_tgt_power(priority=4, power=-50.0, bounds=(None, -50.0), expected=-50.0)
    test_bounds(priority=4, expected_power=-50.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=3, expected_power=-50.0, expected_bounds=(-200.0, -50.0))
    test_bounds(priority=2, expected_power=-50.0, expected_bounds=(-200.0, -50.0))
    test_bounds(priority=1, expected_power=-50.0, expected_bounds=(-200.0, -50.0))

    test_tgt_power(priority=3, power=-0.0, bounds=(-200.0, 200.0), expected=None)
    test_bounds(priority=1, expected_power=-50.0, expected_bounds=(-200.0, -50.0))

    test_tgt_power(priority=1, power=-150.0, bounds=(-200.0, -150.0), expected=-150.0)
    test_bounds(priority=2, expected_power=-150.0, expected_bounds=(-200.0, -50.0))
    test_bounds(priority=1, expected_power=-150.0, expected_bounds=(-200.0, -50.0))

    test_tgt_power(priority=4, power=-180.0, bounds=(-200.0, -50.0), expected=None)
    test_bounds(priority=1, expected_power=-150.0, expected_bounds=(-200.0, -50.0))

    test_tgt_power(priority=4, power=50.0, bounds=(50.0, None), expected=50.0)
    test_bounds(priority=4, expected_power=50.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=3, expected_power=50.0, expected_bounds=(50.0, 200.0))
    test_bounds(priority=2, expected_power=50.0, expected_bounds=(50.0, 200.0))
    test_bounds(priority=1, expected_power=50.0, expected_bounds=(50.0, 200.0))

    test_tgt_power(priority=4, power=0.0, bounds=(-200.0, 200.0), expected=-150.0)
    test_bounds(priority=4, expected_power=-150.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=3, expected_power=-150.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=-150.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=1, expected_power=-150.0, expected_bounds=(-200.0, 200.0))

    test_tgt_power(priority=3, power=0.0, bounds=(-200.0, 200.0), expected=None)
    test_bounds(priority=3, expected_power=-150.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=-150.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=1, expected_power=-150.0, expected_bounds=(-200.0, 200.0))

    test_tgt_power(priority=2, power=50.0, bounds=(-100, 100), expected=-100.0)
    test_bounds(priority=3, expected_power=-100.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=2, expected_power=-100.0, expected_bounds=(-200.0, 200.0))
    test_bounds(priority=1, expected_power=-100.0, expected_bounds=(-100.0, 100.0))

    test_tgt_power(priority=1, power=100.0, bounds=(100, 200), expected=100.0)
    test_bounds(priority=1, expected_power=100.0, expected_bounds=(-100.0, 100.0))

    test_tgt_power(priority=1, power=50.0, bounds=(50, 200), expected=50.0)
    test_bounds(priority=1, expected_power=50.0, expected_bounds=(-100.0, 100.0))

    test_tgt_power(priority=1, power=200.0, bounds=(50, 200), expected=100.0)
    test_bounds(priority=1, expected_power=100.0, expected_bounds=(-100.0, 100.0))

    test_tgt_power(priority=1, power=0.0, bounds=(-200, 200), expected=0.0)
    test_bounds(priority=1, expected_power=0.0, expected_bounds=(-100.0, 100.0))

    test_tgt_power(priority=1, power=None, bounds=(-200, 200), expected=50.0)
    test_bounds(priority=1, expected_power=50.0, expected_bounds=(-100.0, 100.0))
