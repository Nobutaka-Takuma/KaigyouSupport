"""Shared building blocks for the ETL and the API.

Kept deliberately small: configuration loading, a database handle, JIS mesh
geometry and the scoring model. Anything specific to fetching data lives in
``kaigyou_etl``; anything specific to serving it lives in ``kaigyou_api``.
"""
