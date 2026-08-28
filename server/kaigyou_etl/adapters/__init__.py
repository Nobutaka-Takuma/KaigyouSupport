"""Adapter registry.

``config/sources.yaml`` names an adapter per source; this maps that name to a
class. Adding a publisher means adding a module and one entry here.
"""
from __future__ import annotations

from typing import Type

from kaigyou_etl.adapters.base import AdapterContext, SourceAdapter
from kaigyou_etl.adapters.estat_business_mesh import EStatBusinessMeshAdapter
from kaigyou_etl.adapters.estat_daytime_mesh import EStatDaytimeMeshAdapter
from kaigyou_etl.adapters.estat_mesh import EStatMeshAdapter
from kaigyou_etl.adapters.mhlw_clinics import MHLWClinicsAdapter
from kaigyou_etl.adapters.mhlw_specialties import MHLWSpecialtiesAdapter
from kaigyou_etl.adapters.mlit_municipalities import MLITMunicipalitiesAdapter
from kaigyou_etl.adapters.mlit_future_population import MLITFuturePopulationAdapter
from kaigyou_etl.adapters.mlit_land_prices import MLITLandPriceAdapter
from kaigyou_etl.adapters.mlit_stations import MLITStationsAdapter
from kaigyou_etl.adapters.osm_walk_network import OSMWalkNetworkAdapter

ADAPTERS: dict[str, Type[SourceAdapter]] = {
    "mhlw_clinics": MHLWClinicsAdapter,
    "mhlw_specialties": MHLWSpecialtiesAdapter,
    "estat_mesh": EStatMeshAdapter,
    "estat_business_mesh": EStatBusinessMeshAdapter,
    "estat_daytime_mesh": EStatDaytimeMeshAdapter,
    "mlit_stations": MLITStationsAdapter,
    "mlit_municipalities": MLITMunicipalitiesAdapter,
    "mlit_land_prices": MLITLandPriceAdapter,
    "mlit_future_population": MLITFuturePopulationAdapter,
    "osm_walk_network": OSMWalkNetworkAdapter,
}

__all__ = ["ADAPTERS", "AdapterContext", "SourceAdapter"]


def get_adapter(name: str) -> Type[SourceAdapter]:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(f"unknown adapter {name!r}; available: {sorted(ADAPTERS)}") from None
