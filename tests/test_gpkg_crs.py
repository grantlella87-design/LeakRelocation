"""The GeoPackage's coordinate system is stored in a form ArcGIS Pro accepts.

GDAL writes the DNV equidistant conic as GDAL WKT1 (latitude_of_center, AXIS),
which Pro rejects as "The coordinate system description is invalid". The
workflow rewrites it as Esri WKT; these check that the rewrite gives Pro's names
and that GDAL still reads the same CRS back.
"""
import sqlite3

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import Point

from leakrelocation import native_crs

# The DNV layers' CRS as the service publishes it.
DNV_ESRI_WKT = (
    'PROJCS["NG_Equidistant_Conic_USft",GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Equidistant_Conic"],PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",-74.0],'
    'PARAMETER["Standard_Parallel_1",42.0],PARAMETER["Standard_Parallel_2",44.0],'
    'PARAMETER["Latitude_Of_Origin",43.0],UNIT["Foot_US",0.3048006096012192]]'
)


def stored_definitions(path):
    connection = sqlite3.connect(str(path))
    try:
        return dict(connection.execute(
            "SELECT organization, definition FROM gpkg_spatial_ref_sys "
            "WHERE srs_id > 0").fetchall())
    finally:
        connection.close()


def write_gpkg(path):
    crs = CRS.from_wkt(DNV_ESRI_WKT)
    gpd.GeoDataFrame({"OrigLeakOID": [1, 2]},
                     geometry=[Point(783699.5, -206231.3), Point(10.0, 20.0)],
                     crs=crs).to_file(str(path), layer="relocated_leaks",
                                      driver="GPKG")
    return crs


class TestEsriWkt:
    def test_uses_esri_parameter_and_unit_names(self):
        wkt = native_crs.esri_wkt(CRS.from_wkt(DNV_ESRI_WKT))
        assert 'PARAMETER["Central_Meridian",-74.0]' in wkt
        assert 'PARAMETER["Latitude_Of_Origin",43.0]' in wkt
        assert 'UNIT["Foot_US",0.3048006096012192]' in wkt
        assert "latitude_of_center" not in wkt
        assert "AXIS[" not in wkt and "AUTHORITY[" not in wkt

    def test_describes_the_same_crs(self):
        crs = CRS.from_wkt(DNV_ESRI_WKT)
        assert CRS.from_wkt(native_crs.esri_wkt(crs)).equals(
            crs, ignore_axis_order=True)


class TestGpkgRewrite:
    def test_gdal_writes_the_form_pro_rejects(self, tmp_path):
        # The reason the rewrite exists. If GDAL ever writes Esri names itself,
        # this fails and the rewrite can go.
        path = tmp_path / "out.gpkg"
        write_gpkg(path)
        custom = [d for org, d in stored_definitions(path).items()
                  if org.upper() != "EPSG"]
        assert custom and "Central_Meridian" not in custom[0]

    def test_rewrites_custom_crs_and_reads_back_the_same(self, tmp_path):
        path = tmp_path / "out.gpkg"
        crs = write_gpkg(path)

        assert native_crs.make_gpkg_crs_readable_by_arcgis(path) == 1

        custom = [d for org, d in stored_definitions(path).items()
                  if org.upper() != "EPSG"]
        assert custom == [native_crs.esri_wkt(crs)]
        back = gpd.read_file(str(path), layer="relocated_leaks")
        assert back.crs.equals(crs, ignore_axis_order=True)
        assert list(back.geometry.x) == [783699.5, 10.0]

    def test_second_run_changes_nothing(self, tmp_path):
        path = tmp_path / "out.gpkg"
        write_gpkg(path)
        native_crs.make_gpkg_crs_readable_by_arcgis(path)
        assert native_crs.make_gpkg_crs_readable_by_arcgis(path) == 0

    def test_epsg_records_are_left_alone(self, tmp_path):
        path = tmp_path / "out.gpkg"
        write_gpkg(path)
        before = stored_definitions(path).get("EPSG")
        native_crs.make_gpkg_crs_readable_by_arcgis(path)
        assert stored_definitions(path).get("EPSG") == before
