"""Read-only HTTP API over the PostGIS database.

The API never fetches external data: it serves whatever ``kaigyou-etl`` has
managed to load, and reports honestly when that is nothing.
"""
