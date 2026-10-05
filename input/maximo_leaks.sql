-- Enterprise (Maximo) leaks, for scripts/maximo_leaks.py
--
-- THIS IS A TEMPLATE. The table and column names below are placeholders and
-- will not run against the real database. Replace everything after the column
-- aliases; the aliases themselves are the contract with the rest of the
-- project and must stay exactly as they are.
--
-- To use your own copy instead of editing this one in place:
--     set LEAKRELOCATION_MAXIMO_QUERY=C:\path\to\your_query.sql
--
-- Connection, separately, and never in a file:
--     set LEAKRELOCATION_MAXIMO_DSN=Driver={ODBC Driver 18 for SQL Server};Server=...
--
-- ---------------------------------------------------------------------------
-- REQUIRED aliases. A query missing any of these is refused by name, before
-- anything is downloaded or matched.
--
--   LeakId      Unique per leak. Everything is keyed on it, and a second row
--               with the same value is skipped and counted - it is not merged.
--   X, Y        The leak's position, in the CRS named by
--               LEAKRELOCATION_MAXIMO_CRS (default EPSG:4326, longitude then
--               latitude). If these are MA state plane feet, set that variable
--               to EPSG:2249. Getting it wrong puts every leak in the wrong
--               place and the run still finishes.
--   Material    The leaking pipe's material, as text. Matched against the
--               decoded ASSETTYPE of the candidate pipe, exactly or by family.
--   Diameter    Nominal diameter in inches, as a number.
--
-- OPTIONAL aliases. Each one left out costs something the run will name:
--
--   LeakNumber      what a person calls the leak. Falls back to LeakId.
--   FacilityType    text containing "service", or "main"/"distribution".
--                   Decides which pipe layers the leak may relocate onto;
--                   without it every leak is searched against both.
--   LeakDate        the date the relocation is checked against the pipe's
--                   life. Without it the date rule decides nothing.
--   Pressure        advisory unless REQUIRE_PRESSURE_MATCH is set.
--   Address         street address.
--   City            municipality.
--   NearestXStreet  cross street.
--
-- Return MA leaks only, or whatever scope this run is meant to cover: there is
-- no jurisdiction filter applied after the query. The pipe layers are filtered
-- to jurisdiction = 'MA', so a leak outside that has nothing to match against.
-- ---------------------------------------------------------------------------

SELECT
    wo.wonum                        AS LeakId,
    wo.wonum                        AS LeakNumber,
    loc.longitude_x                 AS X,
    loc.latitude_y                  AS Y,
    leak.pipe_material              AS Material,
    leak.pipe_diameter_in           AS Diameter,
    leak.facility_type              AS FacilityType,
    leak.operating_pressure         AS Pressure,
    wo.reportdate                   AS LeakDate,
    loc.street_address              AS Address,
    loc.city                        AS City,
    loc.cross_street                AS NearestXStreet
FROM        maximo.workorder        AS wo
INNER JOIN  maximo.gasleak          AS leak ON leak.wonum   = wo.wonum
LEFT  JOIN  maximo.worklocation     AS loc  ON loc.location = wo.location
WHERE       wo.worktype             = 'LEAK'
  AND       wo.siteid               IN ('MA')
  AND       loc.longitude_x         IS NOT NULL
  AND       loc.latitude_y          IS NOT NULL
;
