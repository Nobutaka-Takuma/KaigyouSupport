# データ取得状況レポート

生成: `kaigyou-etl status` / 2026-08-18T11:28:38Z

```
データ取得状況
==============================================================================
[FAILED] estat_population_mesh      official rows=      0
         国勢調査 3次メッシュ（1kmメッシュ）人口・世帯
         理由: download: network_blocked: could not reach https://www.e-stat.go.jp/gis/statmap-search/data: ProxyError: 403 Forbidden
         URL : https://www.e-stat.go.jp/gis/statmap-search/data
[FAILED] mhlw_dental_clinics        official rows=      0
         医療情報ネット（オープンデータ）歯科診療所
         理由: download: network_blocked: could not reach https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2300/downloadCsv?prefectureCode=13&facilityKind=3: ProxyError: 403 Forbidden
         URL : https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2300/downloadCsv?prefectureCode=13&facilityKind=3
[FAILED] mlit_municipalities        official rows=      0
         国土数値情報 行政区域（N03）
         理由: download: network_blocked: could not reach https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2023/N03-20230101_13_GML.zip: ProxyError: 403 Forbidden
         URL : https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2023/N03-20230101_13_GML.zip
[FAILED] mlit_stations              official rows=      0
         国土数値情報 駅別乗降客数（S12）
         理由: download: network_blocked: could not reach https://nlftp.mlit.go.jp/ksj/gml/data/S12/S12-23/S12-23_GML.zip: ProxyError: 403 Forbidden
         URL : https://nlftp.mlit.go.jp/ksj/gml/data/S12/S12-23/S12-23_GML.zip
[OK    ] sample_dental_clinics      SAMPLE   rows=   1983
         【サンプル】歯科診療所（合成データ）
[OK    ] sample_municipalities      SAMPLE   rows=     30
         【サンプル】行政区域（合成データ）
[OK    ] sample_population_mesh     SAMPLE   rows=   1147
         【サンプル】1kmメッシュ人口（合成データ）
[OK    ] sample_stations            SAMPLE   rows=    230
         【サンプル】駅・乗降客数（合成データ）
==============================================================================
公的データを取得できた情報源: 0 / 4
⚠ サンプル（合成）データが投入されています。実データではありません。
```
