"""Installed and reserved M02 capability declarations."""

from importlib.util import find_spec

from mobile_sensing.contracts import CapabilityDescriptor, CapabilityRegistry
from mobile_sensing.contracts.configuration import EnvironmentProviderRequest, RegularGridConfig


def environment_capability_registry() -> CapabilityRegistry:
    from mobile_sensing.application.studio_models import EnvironmentEditor
    from mobile_sensing.application.project_models import DispatchEditor

    osm_available = find_spec("osmnx") is not None
    optimization_available = find_spec("ortools") is not None
    capabilities = (
        CapabilityDescriptor(
            key="dispatch.batch_nearest_matching@1",
            available=True,
            implementation_version="dispatch.batch_nearest_matching@1",
            parameter_schema=DispatchEditor.model_json_schema(),
        ),
        CapabilityDescriptor(
            key="dispatch.one_shot@1",
            available=optimization_available,
            implementation_version="dispatch.one_shot@1" if optimization_available else None,
            unavailable_reason=(
                None
                if optimization_available
                else "Install the optimization extra to enable One-shot."
            ),
            parameter_schema=DispatchEditor.model_json_schema(),
        ),
        CapabilityDescriptor(
            key="environment.local_files@1",
            available=True,
            implementation_version="local-files@1",
            parameter_schema=EnvironmentProviderRequest.model_json_schema(mode="validation"),
        ),
        CapabilityDescriptor(
            key="environment.osm@1",
            available=False,
            implementation_version=None,
            parameter_schema=EnvironmentProviderRequest.model_json_schema(mode="validation"),
            unavailable_reason="Live OSM acquisition is deferred; use supplied local roads.",
        ),
        CapabilityDescriptor(
            key="environment.studio_osm@1",
            available=osm_available,
            implementation_version="osmnx-editor@1" if osm_available else None,
            parameter_schema=EnvironmentEditor.model_json_schema(mode="validation"),
            unavailable_reason=(
                None if osm_available else "Install the geography extra to enable OSM acquisition."
            ),
        ),
        CapabilityDescriptor(
            key="grid.regular@1",
            available=True,
            implementation_version="metric-grid@1",
            parameter_schema=RegularGridConfig.model_json_schema(mode="validation"),
        ),
    )
    return CapabilityRegistry(registry_version="m02@1", capabilities=capabilities)
