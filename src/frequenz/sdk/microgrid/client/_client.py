# License: MIT
# Copyright © 2022 Frequenz Energy-as-a-Service GmbH

"""Client for requests to the Microgrid API."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar, cast

import grpc
from frequenz.api.common import components_pb2 as components_pb
from frequenz.api.common import metrics_pb2 as metrics_pb
from frequenz.api.microgrid import microgrid_pb2 as microgrid_pb
from frequenz.api.microgrid.microgrid_pb2_grpc import MicrogridStub
from frequenz.channels import Broadcast, Receiver, Sender
from google.protobuf.empty_pb2 import Empty  # pylint: disable=no-name-in-module

from ..._internal._constants import RECEIVER_MAX_SIZE
from ..component import (
    BatteryData,
    Component,
    ComponentCategory,
    ComponentId,
    EVChargerData,
    InverterData,
    MeterData,
)
from ..component._component import (
    _component_category_from_protobuf,
    _component_metadata_from_protobuf,
    _component_type_from_protobuf,
)
from ..metadata import Location, Metadata
from ._connection import Connection
from ._retry import LinearBackoff, RetryStrategy

DEFAULT_GRPC_CALL_TIMEOUT = 60.0
"""The default timeout for gRPC calls made by this client (in seconds)."""

# A generic type for representing various component data types, used in the
# generic function `MicrogridGrpcClient._component_data_task` that fetches
# component data and transforms it into one of the specific types.
_GenericComponentData = TypeVar(
    "_GenericComponentData",
    MeterData,
    BatteryData,
    InverterData,
    EVChargerData,
)
"""Type variable for representing various component data types."""

_logger = logging.getLogger(__name__)


class MicrogridApiClient(ABC):
    """Base interface for microgrid API clients to implement."""

    @abstractmethod
    async def components(self) -> Iterable[Component]:
        """Fetch all the components present in the microgrid.

        Returns:
            Iterator whose elements are all the components in the microgrid.
        """

    @abstractmethod
    async def metadata(self) -> Metadata:
        """Fetch the microgrid metadata.

        Returns:
            the microgrid metadata.
        """

    @abstractmethod
    async def connections(
        self,
        starts: set[int] | None = None,
        ends: set[int] | None = None,
    ) -> Iterable[Connection]:
        """Fetch the connections between components in the microgrid.

        Args:
            starts: if set and non-empty, only include connections whose start
                value matches one of the provided component IDs
            ends: if set and non-empty, only include connections whose end value
                matches one of the provided component IDs

        Returns:
            Microgrid connections matching the provided start and end filters.
        """

    @abstractmethod
    async def meter_data(
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[MeterData]:
        """Return a channel receiver that provides a `MeterData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Args:
            component_id: id of the meter to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime meter data.
        """

    @abstractmethod
    async def battery_data(
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[BatteryData]:
        """Return a channel receiver that provides a `BatteryData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Args:
            component_id: id of the battery to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime battery data.
        """

    @abstractmethod
    async def inverter_data(
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[InverterData]:
        """Return a channel receiver that provides an `InverterData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Args:
            component_id: id of the inverter to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime inverter data.
        """

    @abstractmethod
    async def ev_charger_data(
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[EVChargerData]:
        """Return a channel receiver that provides an `EvChargeData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Args:
            component_id: id of the ev charger to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime ev charger data.
        """

    @abstractmethod
    async def set_power(self, component_id: ComponentId, power_w: float) -> None:
        """Send request to the Microgrid to set power for component.

        If power > 0, then component will be charged with this power.
        If power < 0, then component will be discharged with this power.
        If power == 0, then stop charging or discharging component.


        Args:
            component_id: id of the component to set power.
            power_w: power to set for the component.
        """

    @abstractmethod
    async def set_bounds(
        self, component_id: ComponentId, lower: float, upper: float
    ) -> None:
        """Send `SetBoundsParam`s received from a channel to the Microgrid service.

        Args:
            component_id: ID of the component to set bounds for.
            lower: Lower bound to be set for the component.
            upper: Upper bound to be set for the component.
        """


# pylint: disable=no-member


class MicrogridGrpcClient(MicrogridApiClient):
    """Microgrid API client implementation using gRPC as the underlying protocol."""

    def __init__(
        self,
        grpc_channel: grpc.aio.Channel,
        target: str,
        retry_spec: RetryStrategy = LinearBackoff(),
    ) -> None:
        """Initialize the class instance.

        Args:
            grpc_channel: asyncio-supporting gRPC channel
            target: server (host:port) to be used for asyncio-supporting gRPC
                channel that the client should use to contact the API
            retry_spec: Specs on how to retry if the connection to a streaming
                method gets lost.
        """
        self.target = target
        """The location (as "host:port") of the microgrid API gRPC server."""

        self.api = MicrogridStub(grpc_channel)
        """The gRPC stub for the microgrid API."""

        self._component_streams: dict[int, Broadcast[Any]] = {}
        self._streaming_tasks: dict[int, asyncio.Task[None]] = {}
        self._retry_spec = retry_spec

    async def components(self) -> Iterable[Component]:
        """Fetch all the components present in the microgrid.

        Returns:
            Iterator whose elements are all the components in the microgrid.

        Raises:
            AioRpcError: if connection to Microgrid API cannot be established or
                when the api call exceeded timeout
        """
        try:
            # grpc.aio is missing types and mypy thinks this is not awaitable,
            # but it is
            component_list = await cast(
                Awaitable[microgrid_pb.ComponentList],
                self.api.ListComponents(
                    microgrid_pb.ComponentFilter(),
                    timeout=int(DEFAULT_GRPC_CALL_TIMEOUT),
                ),
            )

        except grpc.aio.AioRpcError as err:
            msg = f"Failed to list components. Microgrid API: {self.target}. Err: {err.details()}"
            raise grpc.aio.AioRpcError(
                code=err.code(),
                initial_metadata=err.initial_metadata(),
                trailing_metadata=err.trailing_metadata(),
                details=msg,
                debug_error_string=err.debug_error_string(),
            )
        components_only = filter(
            lambda c: c.category
            is not components_pb.ComponentCategory.COMPONENT_CATEGORY_SENSOR,
            component_list.components,
        )
        result: Iterable[Component] = map(
            lambda c: Component(
                c.id,
                _component_category_from_protobuf(c.category),
                _component_type_from_protobuf(c.category, c.inverter),
                _component_metadata_from_protobuf(c.category, c.grid),
            ),
            components_only,
        )

        return result

    async def metadata(self) -> Metadata:
        """Fetch the microgrid metadata.

        If there is an error fetching the metadata, the microgrid ID and
        location will be set to None.

        Returns:
            the microgrid metadata.
        """
        microgrid_metadata: microgrid_pb.MicrogridMetadata | None = None
        try:
            microgrid_metadata = await cast(
                Awaitable[microgrid_pb.MicrogridMetadata],
                self.api.GetMicrogridMetadata(
                    Empty(),
                    timeout=int(DEFAULT_GRPC_CALL_TIMEOUT),
                ),
            )
        except grpc.aio.AioRpcError:
            _logger.exception("The microgrid metadata is not available.")

        if not microgrid_metadata:
            return Metadata()

        location: Location | None = None
        if microgrid_metadata.location:
            location = Location(
                latitude=microgrid_metadata.location.latitude,
                longitude=microgrid_metadata.location.longitude,
            )

        return Metadata(microgrid_id=microgrid_metadata.microgrid_id, location=location)

    async def connections(
        self,
        starts: set[int] | None = None,
        ends: set[int] | None = None,
    ) -> Iterable[Connection]:
        """Fetch the connections between components in the microgrid.

        Args:
            starts: if set and non-empty, only include connections whose start
                value matches one of the provided component IDs
            ends: if set and non-empty, only include connections whose end value
                matches one of the provided component IDs

        Returns:
            Microgrid connections matching the provided start and end filters.

        Raises:
            AioRpcError: if connection to Microgrid API cannot be established or
                when the api call exceeded timeout
        """
        connection_filter = microgrid_pb.ConnectionFilter(starts=starts, ends=ends)
        try:
            valid_components, all_connections = await asyncio.gather(
                self.components(),
                # grpc.aio is missing types and mypy thinks this is not
                # awaitable, but it is
                cast(
                    Awaitable[microgrid_pb.ConnectionList],
                    self.api.ListConnections(
                        connection_filter,
                        timeout=int(DEFAULT_GRPC_CALL_TIMEOUT),
                    ),
                ),
            )
        except grpc.aio.AioRpcError as err:
            msg = f"Failed to list connections. Microgrid API: {self.target}. Err: {err.details()}"
            raise grpc.aio.AioRpcError(
                code=err.code(),
                initial_metadata=err.initial_metadata(),
                trailing_metadata=err.trailing_metadata(),
                details=msg,
                debug_error_string=err.debug_error_string(),
            )
        # Filter out the components filtered in `components` method.
        # id=0 is an exception indicating grid component.
        valid_ids = {c.component_id for c in valid_components}
        valid_ids.add(0)

        connections = filter(
            lambda c: (c.start in valid_ids and c.end in valid_ids),
            all_connections.connections,
        )

        result: Iterable[Connection] = map(
            lambda c: Connection(c.start, c.end), connections
        )

        return result

    async def _component_data_task(
        self,
        component_id: ComponentId,
        transform: Callable[[microgrid_pb.ComponentData], _GenericComponentData],
        sender: Sender[_GenericComponentData],
    ) -> None:
        """Read data from the microgrid API and send to a channel.

        Args:
            component_id: id of the component to get data for.
            transform: A method for transforming raw component data into the
                desired output type.
            sender: A channel sender, to send the component data to.
        """
        retry_spec: RetryStrategy = self._retry_spec.copy()
        while True:
            _logger.debug(
                "Making call to `GetComponentData`, for component_id=%d", component_id
            )
            try:
                call = self.api.StreamComponentData(
                    microgrid_pb.ComponentIdParam(id=component_id),
                )
                # grpc.aio is missing types and mypy thinks this is not
                # async iterable, but it is
                async for msg in call:  # type: ignore[attr-defined]
                    await sender.send(transform(msg))
            except grpc.aio.AioRpcError as err:
                api_details = f"Microgrid API: {self.target}."
                _logger.exception(
                    "`GetComponentData`, for component_id=%d: exception: %s api: %s",
                    component_id,
                    err,
                    api_details,
                )

            if interval := retry_spec.next_interval():
                _logger.warning(
                    "`GetComponentData`, for component_id=%d: connection ended, "
                    "retrying %s in %0.3f seconds.",
                    component_id,
                    retry_spec.get_progress(),
                    interval,
                )
                await asyncio.sleep(interval)
            else:
                _logger.warning(
                    "`GetComponentData`, for component_id=%d: connection ended, "
                    "retry limit exceeded %s.",
                    component_id,
                    retry_spec.get_progress(),
                )
                break

    def _get_component_data_channel(
        self,
        component_id: ComponentId,
        transform: Callable[[microgrid_pb.ComponentData], _GenericComponentData],
    ) -> Broadcast[_GenericComponentData]:
        """Return the broadcast channel for a given component_id.

        If a broadcast channel for the given component_id doesn't exist, create
        a new channel and a task for reading data from the microgrid api and
        sending them to the channel.

        Args:
            component_id: id of the component to get data for.
            transform: A method for transforming raw component data into the
                desired output type.

        Returns:
            The channel for the given component_id.
        """
        if component_id in self._component_streams:
            return self._component_streams[component_id]
        task_name = f"raw-component-data-{component_id}"
        chan = Broadcast[_GenericComponentData](task_name)
        self._component_streams[component_id] = chan

        self._streaming_tasks[component_id] = asyncio.create_task(
            self._component_data_task(
                component_id,
                transform,
                chan.new_sender(),
            ),
            name=task_name,
        )
        return chan

    async def _expect_category(
        self,
        component_id: ComponentId,
        expected_category: ComponentCategory,
    ) -> None:
        """Check if the given component_id is of the expected type.

        Raises:
            ValueError: if the given id is unknown or has a different type.

        Args:
            component_id: Component id to check.
            expected_category: Component category that the given id is expected
                to have.
        """
        try:
            comp = next(
                comp
                for comp in await self.components()
                if comp.component_id == component_id
            )
        except StopIteration as exc:
            raise ValueError(
                f"Unable to find component with id {component_id}"
            ) from exc

        if comp.category != expected_category:
            raise ValueError(
                f"Component id {component_id} is a {comp.category}"
                f", not a {expected_category}."
            )

    async def meter_data(  # noqa: DOC502 (ValueError is raised indirectly by _expect_category)
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[MeterData]:
        """Return a channel receiver that provides a `MeterData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Raises:
            ValueError: if the given id is unknown or has a different type.

        Args:
            component_id: id of the meter to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime meter data.
        """
        await self._expect_category(
            component_id,
            ComponentCategory.METER,
        )
        return self._get_component_data_channel(
            component_id,
            MeterData.from_proto,
        ).new_receiver(maxsize=maxsize)

    async def battery_data(  # noqa: DOC502 (ValueError is raised indirectly by _expect_category)
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[BatteryData]:
        """Return a channel receiver that provides a `BatteryData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Raises:
            ValueError: if the given id is unknown or has a different type.

        Args:
            component_id: id of the battery to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime battery data.
        """
        await self._expect_category(
            component_id,
            ComponentCategory.BATTERY,
        )
        return self._get_component_data_channel(
            component_id,
            BatteryData.from_proto,
        ).new_receiver(maxsize=maxsize)

    async def inverter_data(  # noqa: DOC502 (ValueError is raised indirectly by _expect_category)
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[InverterData]:
        """Return a channel receiver that provides an `InverterData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Raises:
            ValueError: if the given id is unknown or has a different type.

        Args:
            component_id: id of the inverter to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime inverter data.
        """
        await self._expect_category(
            component_id,
            ComponentCategory.INVERTER,
        )
        return self._get_component_data_channel(
            component_id,
            InverterData.from_proto,
        ).new_receiver(maxsize=maxsize)

    async def ev_charger_data(  # noqa: DOC502 (ValueError is raised indirectly by _expect_category)
        self,
        component_id: ComponentId,
        maxsize: int = RECEIVER_MAX_SIZE,
    ) -> Receiver[EVChargerData]:
        """Return a channel receiver that provides an `EvChargeData` stream.

        If only the latest value is required, the `Receiver` returned by this
        method can be converted into a `Peekable` with the `into_peekable`
        method on the `Receiver.`

        Raises:
            ValueError: if the given id is unknown or has a different type.

        Args:
            component_id: id of the ev charger to get data for.
            maxsize: Size of the receiver's buffer.

        Returns:
            A channel receiver that provides realtime ev charger data.
        """
        await self._expect_category(
            component_id,
            ComponentCategory.EV_CHARGER,
        )
        return self._get_component_data_channel(
            component_id,
            EVChargerData.from_proto,
        ).new_receiver(maxsize=maxsize)

    async def set_power(self, component_id: ComponentId, power_w: float) -> None:
        """Send request to the Microgrid to set power for component.

        If power > 0, then component will be charged with this power.
        If power < 0, then component will be discharged with this power.
        If power == 0, then stop charging or discharging component.


        Args:
            component_id: id of the component to set power.
            power_w: power to set for the component.

        Raises:
            AioRpcError: if connection to Microgrid API cannot be established or
                when the api call exceeded timeout
        """
        try:
            await cast(
                Awaitable[microgrid_pb.SetPowerActiveParam],
                self.api.SetPowerActive(
                    microgrid_pb.SetPowerActiveParam(
                        component_id=component_id, power=power_w
                    ),
                    timeout=int(DEFAULT_GRPC_CALL_TIMEOUT),
                ),
            )
        except grpc.aio.AioRpcError as err:
            msg = f"Failed to set power. Microgrid API: {self.target}. Err: {err.details()}"
            raise grpc.aio.AioRpcError(
                code=err.code(),
                initial_metadata=err.initial_metadata(),
                trailing_metadata=err.trailing_metadata(),
                details=msg,
                debug_error_string=err.debug_error_string(),
            )

    async def set_bounds(
        self,
        component_id: ComponentId,
        lower: float,
        upper: float,
    ) -> None:
        """Send `SetBoundsParam`s received from a channel to the Microgrid service.

        Args:
            component_id: ID of the component to set bounds for.
            lower: Lower bound to be set for the component.
            upper: Upper bound to be set for the component.

        Raises:
            ValueError: when upper bound is less than 0, or when lower bound is
                greater than 0.
            grpc.aio.AioRpcError: if connection to Microgrid API cannot be established
                or when the api call exceeded timeout
        """
        api_details = f"Microgrid API: {self.target}."
        if upper < 0:
            raise ValueError(f"Upper bound {upper} must be greater than or equal to 0.")
        if lower > 0:
            raise ValueError(f"Lower bound {upper} must be less than or equal to 0.")

        target_metric = (
            microgrid_pb.SetBoundsParam.TargetMetric.TARGET_METRIC_POWER_ACTIVE
        )
        try:
            self.api.AddInclusionBounds(
                microgrid_pb.SetBoundsParam(
                    component_id=component_id,
                    target_metric=target_metric,
                    bounds=metrics_pb.Bounds(lower=lower, upper=upper),
                ),
            )
        except grpc.aio.AioRpcError as err:
            _logger.error(
                "set_bounds write failed: %s, for message: %s, api: %s. Err: %s",
                err,
                next,
                api_details,
                err.details(),
            )
            raise
