/** MapLibre style resolution.
 *
 * No tiles are bundled with this project. If neither a style URL nor a raster
 * template is configured the map still works -- our own layers render over a
 * plain background -- rather than failing to initialise.
 */
import type { StyleSpecification } from "maplibre-gl";

const STYLE_URL = import.meta.env.VITE_BASEMAP_STYLE as string | undefined;
const RASTER = import.meta.env.VITE_RASTER_TILES as string | undefined;
const ATTRIBUTION = import.meta.env.VITE_RASTER_ATTRIBUTION as string | undefined;

const BLANK: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [
    {
      id: "background",
      type: "background",
      paint: { "background-color": "#eef1f4" },
    },
  ],
};

export function basemapStyle(): string | StyleSpecification {
  if (STYLE_URL) return STYLE_URL;
  if (RASTER) {
    return {
      version: 8,
      sources: {
        basemap: {
          type: "raster",
          tiles: [RASTER],
          tileSize: 256,
          attribution: ATTRIBUTION ?? "",
        },
      },
      layers: [
        { id: "background", type: "background", paint: { "background-color": "#eef1f4" } },
        { id: "basemap", type: "raster", source: "basemap" },
      ],
    } satisfies StyleSpecification;
  }
  return BLANK;
}

export const hasBasemap = Boolean(STYLE_URL || RASTER);
