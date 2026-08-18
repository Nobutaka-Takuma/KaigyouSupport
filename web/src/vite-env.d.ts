/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_BASEMAP_STYLE?: string;
  readonly VITE_RASTER_TILES?: string;
  readonly VITE_RASTER_ATTRIBUTION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
