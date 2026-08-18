/** Client-side geometry, used only for drawing (never for analysis).
 *
 * The trade-area circle shown on the map is a visual approximation of the
 * buffer PostGIS actually uses; all reported figures come from the API.
 */
export function circlePolygon(
  lng: number,
  lat: number,
  radiusMetres: number,
  steps = 96,
): GeoJSON.Feature<GeoJSON.Polygon> {
  const coords: [number, number][] = [];
  const latRad = (lat * Math.PI) / 180;
  const dLat = radiusMetres / 111_320;
  const dLng = radiusMetres / (111_320 * Math.cos(latRad));
  for (let i = 0; i <= steps; i += 1) {
    const theta = (i / steps) * 2 * Math.PI;
    coords.push([lng + dLng * Math.cos(theta), lat + dLat * Math.sin(theta)]);
  }
  return {
    type: "Feature",
    geometry: { type: "Polygon", coordinates: [coords] },
    properties: { radius_m: radiusMetres },
  };
}

export const EMPTY_FC: GeoJSON.FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};
