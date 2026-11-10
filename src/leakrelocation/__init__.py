"""Relocates gas leak points onto the pipe they were on.

Importing this package has no side effects: it opens no network connections,
creates no folders and reads no data. The modules, roughly in the order a run
uses them:

    config         every setting, read from the environment once
    auth           ArcGIS sign-in and the cached token
    arcgis         requests to the REST layers
    layer_fields   which fields are read from each layer
    layer_cache    the on-disk copy of each layer, and the download behind it
    native_crs     each layer's own coordinate system, and the one used to match
    dates          epoch-millisecond and SQL timestamp conversions
    assettype      naming a pipe's material from its subtype domain
    matching       the rules: material, diameter, pressure, pipe life
    workflow       the relocation itself
    maximo_sql     the enterprise leaks, read from SQL
    leak_location  where a leak is, in words
    schema         the names of the columns and layers this project writes
    output         console output and stage timings
    distance_report, distance_dashboard   how far the leaks moved
    apply_geometry  writing relocated positions back to the leak layer
    map_server, viewer_pane   the local map
"""

__version__ = "1.0.0"
