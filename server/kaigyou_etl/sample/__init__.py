"""Synthetic development data.

**Nothing in this package is real.** It exists so the pipeline, the analysis
SQL and the UI can be exercised end to end on a machine that has no access to
the publishers' data. Every row it writes is attributed to a ``data_sources``
entry with ``dataset_kind = 'sample'``, whose name starts with 【サンプル】,
and the API refuses to hide that: ``GET /api/data-status`` reports
``contains_sample_data``, and the web client shows a standing banner whenever
it is true.

Real data replaces it by running the corresponding adapter; the sample sources
can be dropped entirely with ``kaigyou-etl drop-sample``.
"""
